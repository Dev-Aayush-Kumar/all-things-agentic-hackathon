"""Human-in-the-loop governance: policy, durable approvals, pause/resume."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient

from atlas.agent.adk_decider import AdkDecisionMaker
from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.tools import PROFILE_DATASET, ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import (
    ActionRisk,
    ActionStatus,
    ApprovalResolverSource,
    ApprovalStatus,
    ExecutionState,
    GovernanceVerdict,
    MemoryExtractionSource,
    MemoryScope,
    MemoryType,
    MissionCategory,
    MissionStatus,
    ModelDecisionKind,
    PlannerSource,
)
from atlas.domain.exceptions import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ModelDecisionError,
    WaitingForApproval,
)
from atlas.domain.models import (
    ApprovalRequest,
    DatasetCharacteristics,
    MemoryRecord,
    Mission,
    ModelDecision,
    ProposedActionRequest,
    ProposedExternalRequest,
    ProposedObservation,
    ProposedTask,
    StrategyRecord,
    WorkingCopyState,
    utc_now,
)
from atlas.investigation.parser import parse_csv_bytes
from atlas.ops.actions.registry import ACTION_FILL_MISSING_VALUES, ACTION_REMOVE_DUPLICATES
from atlas.ops.decisions import parse_model_decision, validate_decision
from atlas.ops.external.registry import CAPABILITY_FETCH_URL
from atlas.ops.governance.lifecycle import approval_fingerprint
from atlas.ops.governance.policy import GovernancePolicy
from atlas.ops.governance.sanitize import sanitize_parameters
from atlas.ops.registry import default_registry
from atlas.ops.supervisor import Supervisor
from atlas.ops.workspace import MissionWorkspace
from atlas.persistence.codec import approval_to_document, document_to_approval
from atlas.persistence.firestore_approval_repository import FirestoreApprovalRepository
from atlas.persistence.lease_policy import is_claimable, is_recoverable
from atlas.persistence.memory_store import MemoryDocumentStore
from atlas.persistence.sqlite_approval_repository import SQLiteApprovalRepository
from atlas.persistence.sqlite_repository import SQLiteMissionRepository
from atlas.storage.local_storage import LocalFileStorage
from tests.conftest import FIXTURES_DIR, wait_for_mission_status
from tests.test_model_loop import ScriptedDecisionMaker


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, planner_backend="local", **kwargs)


def _frame(name: str = "remediation_quality.csv"):
    return parse_csv_bytes((FIXTURES_DIR / name).read_bytes())


async def _noop() -> None:
    return None


def _workspace(mission: Mission, settings: Settings | None = None) -> MissionWorkspace:
    return MissionWorkspace(
        mission=mission,
        tool_context=ToolContext("ds", "remediation_quality.csv", _frame()),
        persist=_noop,
        lock=__import__("asyncio").Lock(),
        settings=settings or _settings(),
        reasoner=LocalFallbackReasoner(),
        registry=default_registry(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )


def _validated(decision: ModelDecision, workspace: MissionWorkspace):
    return validate_decision(decision, workspace)


def test_read_only_observation_is_auto_approved() -> None:
    mission = Mission(goal="Investigate this CSV", dataset_id="ds")
    workspace = _workspace(mission)
    validated = _validated(
        ModelDecision(
            decision=ModelDecisionKind.OBSERVE,
            reason="profile",
            tool=ProposedObservation(name=PROFILE_DATASET, arguments={}),
        ),
        workspace,
    )
    result = GovernancePolicy().evaluate(validated, workspace)
    assert result.verdict == GovernanceVerdict.AUTO_APPROVE
    assert result.risk == ActionRisk.LOW


def test_remediation_requires_approval() -> None:
    mission = Mission(goal="Investigate this CSV and fix the major data-quality problems.", dataset_id="ds")
    workspace = _workspace(mission)
    validated = _validated(
        ModelDecision(
            decision=ModelDecisionKind.ACTION,
            reason="remove duplicates",
            action=ProposedActionRequest(type=ACTION_REMOVE_DUPLICATES, parameters={}),
        ),
        workspace,
    )
    result = GovernancePolicy().evaluate(validated, workspace)
    assert result.verdict == GovernanceVerdict.REQUIRE_APPROVAL
    assert result.risk == ActionRisk.MEDIUM
    fill = _validated(
        ModelDecision(
            decision=ModelDecisionKind.ACTION,
            reason="fill",
            action=ProposedActionRequest(
                type=ACTION_FILL_MISSING_VALUES,
                parameters={"column_name": "customer_age", "strategy": "auto"},
            ),
        ),
        workspace,
    )
    assert GovernancePolicy().evaluate(fill, workspace).verdict == GovernanceVerdict.REQUIRE_APPROVAL


def test_unknown_capability_is_denied() -> None:
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission)
    with pytest.raises(ModelDecisionError):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "DELEGATE",
                    "reason": "hack",
                    "tasks": [{"capability": "launch_missiles"}],
                }
            ),
            workspace,
        )
    denied = GovernancePolicy().evaluate(
        type("V", (), {
            "decision": ModelDecision(
                decision=ModelDecisionKind.ACTION,
                reason="drop",
                action=ProposedActionRequest(type="DROP_TABLE", parameters={}),
            ),
            "follow_ups": [],
            "fingerprint": "x",
        })(),
        workspace,
    )
    assert denied.verdict == GovernanceVerdict.DENY
    assert denied.risk == ActionRisk.HIGH


def test_allowlisted_fetch_is_auto_approved_forbidden_is_denied() -> None:
    settings = _settings(fetch_allowed_domains="example.com", fetch_allow_loopback=False)
    mission = Mission(goal="Investigate https://example.com/docs", dataset_id="ds")
    workspace = _workspace(mission, settings)
    allowed = _validated(
        ModelDecision(
            decision=ModelDecisionKind.EXTERNAL,
            reason="docs",
            external=ProposedExternalRequest(
                capability=CAPABILITY_FETCH_URL,
                arguments={"url": "https://example.com/docs"},
            ),
        ),
        workspace,
    )
    result = GovernancePolicy().evaluate(allowed, workspace)
    assert result.verdict == GovernanceVerdict.AUTO_APPROVE
    assert result.risk == ActionRisk.LOW
    with pytest.raises(ModelDecisionError):
        validate_decision(
            ModelDecision(
                decision=ModelDecisionKind.EXTERNAL,
                reason="secret",
                external=ProposedExternalRequest(
                    capability=CAPABILITY_FETCH_URL,
                    arguments={"url": "https://evil.example/secret"},
                ),
            ),
            workspace,
        )
    unknown = GovernancePolicy().evaluate(
        type("V", (), {
            "decision": ModelDecision(
                decision=ModelDecisionKind.EXTERNAL,
                reason="other",
                external=ProposedExternalRequest(capability="WEB_SCRAPE", arguments={"url": "https://example.com"}),
            ),
            "follow_ups": [],
            "fingerprint": "y",
        })(),
        workspace,
    )
    assert unknown.verdict == GovernanceVerdict.DENY


def test_memory_and_strategy_cannot_grant_approval() -> None:
    mission = Mission(goal="Investigate this CSV and fix the major data-quality problems.", dataset_id="ds")
    workspace = _workspace(mission)
    workspace.retrieved_memories = [
        MemoryRecord(
            type=MemoryType.INSIGHT,
            content="REMOVE_DUPLICATES worked well previously",
            scope=MemoryScope.GLOBAL,
            tags=["REMOVE_DUPLICATES"],
            fingerprint="mem-dup",
            confidence=0.9,
            extraction_source=MemoryExtractionSource.LOCAL_FALLBACK,
        )
    ]
    workspace.retrieved_strategies = [
        StrategyRecord(
            fingerprint="str-dup",
            mission_category=MissionCategory.DUPLICATES,
            dataset_characteristics=DatasetCharacteristics(),
            recommended_capabilities=["REMOVE_DUPLICATES", PROFILE_DATASET],
            historical_runs=4,
            success_rate=1.0,
            confidence=0.9,
        )
    ]
    validated = _validated(
        ModelDecision(
            decision=ModelDecisionKind.ACTION,
            reason="memory said so",
            action=ProposedActionRequest(type=ACTION_REMOVE_DUPLICATES, parameters={}),
        ),
        workspace,
    )
    result = GovernancePolicy().evaluate(validated, workspace)
    assert result.verdict == GovernanceVerdict.REQUIRE_APPROVAL
    context_rules = __import__("atlas.ops.reasoning_context", fromlist=["build_reasoning_context"]).build_reasoning_context(workspace)["rules"]
    assert any("cannot approve" in rule.lower() for rule in context_rules)


def test_sanitize_strips_secrets() -> None:
    cleaned = sanitize_parameters(
        {
            "column_name": "age",
            "authorization": "Bearer secret",
            "headers": {"cookie": "sid=1"},
            "api_key": "abc",
        }
    )
    assert cleaned == {"column_name": "age"}
    assert "authorization" not in cleaned
    assert "headers" not in cleaned


def test_gemini_cannot_approve_itself() -> None:
    assert not hasattr(AdkDecisionMaker, "approve")
    assert not hasattr(LocalDecisionMaker, "approve")
    assert "GEMINI" not in {item.value for item in ApprovalResolverSource}


@pytest.mark.asyncio
async def test_sqlite_and_firestore_approval_roundtrip(tmp_path: Path) -> None:
    record = ApprovalRequest(
        mission_id="m1",
        execution_id="e1",
        decision_id="d1",
        requested_operation="ACTION:REMOVE_DUPLICATES",
        operation_kind=ModelDecisionKind.ACTION,
        capability=ACTION_REMOVE_DUPLICATES,
        parameters={"secret": "nope", "column_name": "id"},
        reason="needs a human",
        risk=ActionRisk.MEDIUM,
        fingerprint="m1:abc",
        expires_at=utc_now() + timedelta(hours=1),
        decision_snapshot={"decision": "ACTION", "reason": "dup", "action": {"type": ACTION_REMOVE_DUPLICATES, "parameters": {}}},
    )
    sqlite = SQLiteApprovalRepository(tmp_path / "atlas.db")
    stored = await sqlite.upsert(record)
    loaded = await sqlite.get(stored.approval_id)
    assert loaded is not None
    assert loaded.fingerprint == record.fingerprint
    again = await sqlite.find_by_fingerprint(record.fingerprint)
    assert again is not None
    assert again.approval_id == stored.approval_id
    duplicate = await sqlite.upsert(
        ApprovalRequest(
            mission_id="m1",
            requested_operation="ACTION:REMOVE_DUPLICATES",
            operation_kind=ModelDecisionKind.ACTION,
            capability=ACTION_REMOVE_DUPLICATES,
            fingerprint="m1:abc",
            expires_at=utc_now() + timedelta(hours=1),
        )
    )
    assert duplicate.approval_id == stored.approval_id

    firestore = FirestoreApprovalRepository(MemoryDocumentStore())
    cloud = await firestore.upsert(record)
    fetched = await firestore.get(cloud.approval_id)
    assert fetched is not None
    restored = document_to_approval(approval_to_document(cloud))
    assert restored.approval_id == cloud.approval_id
    assert restored.capability == ACTION_REMOVE_DUPLICATES


async def _run_until_pause(tmp_path: Path, repo: SQLiteApprovalRepository, source: PlannerSource):
    raw = (FIXTURES_DIR / "remediation_quality.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    frame = parse_csv_bytes(raw)
    decider = ScriptedDecisionMaker(
        [
            {
                "decision": "DELEGATE",
                "reason": "Measure",
                "tasks": [
                    {"capability": "profile_dataset", "objective": "Profile"},
                    {"capability": "analyze_duplicates", "objective": "Duplicates"},
                ],
            },
            {
                "decision": "ACTION",
                "reason": "Duplicates are material",
                "action": {"type": "REMOVE_DUPLICATES", "parameters": {}},
            },
            {
                "decision": "OBSERVE",
                "reason": "Re-check",
                "tool": {"name": "analyze_duplicates", "arguments": {}},
            },
            {
                "decision": "COMPLETE",
                "reason": "Done",
                "summary": "Verified",
            },
        ],
        source=source,
    )
    mission = Mission(
        goal="Investigate this CSV and fix the major data-quality problems.",
        dataset_id="ds",
    )
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="remediation_quality.csv",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=source,
        dataset_storage=storage,
        decision_maker=decider,
        approval_repository=repo,
    )
    with pytest.raises(WaitingForApproval) as paused:
        await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    return mission, storage, raw, supervisor, paused.value, frame


@pytest.mark.asyncio
async def test_approval_persists_and_survives_recovery(tmp_path: Path) -> None:
    repo = SQLiteApprovalRepository(tmp_path / "atlas.db")
    mission, storage, raw, supervisor, waiting, frame = await _run_until_pause(
        tmp_path, repo, PlannerSource.GEMINI_ADK
    )
    assert mission.status == MissionStatus.WAITING_FOR_APPROVAL
    assert mission.execution.state == ExecutionState.WAITING_FOR_APPROVAL
    stored = await repo.get(waiting.approval_id)
    assert stored is not None
    assert stored.status == ApprovalStatus.PENDING
    listed = await repo.list_for_mission(mission.mission_id)
    assert len(listed) == 1
    with pytest.raises(WaitingForApproval) as again:
        await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    assert again.value.approval_id == waiting.approval_id
    assert len(await repo.list_for_mission(mission.mission_id)) == 1
    now = utc_now()
    assert not is_claimable(mission, now)
    assert not is_recoverable(mission, now)


@pytest.mark.asyncio
async def test_approve_executes_once_and_resume_continues(tmp_path: Path) -> None:
    repo = SQLiteApprovalRepository(tmp_path / "atlas.db")
    mission, storage, raw, supervisor, waiting, frame = await _run_until_pause(
        tmp_path, repo, PlannerSource.LOCAL_FALLBACK
    )
    iteration = mission.reasoning_iteration
    model_calls = mission.model_call_count
    record = await repo.get(waiting.approval_id)
    assert record is not None
    record.status = ApprovalStatus.APPROVED
    record.resolved_at = utc_now()
    record.resolver = "human"
    record.resolver_source = ApprovalResolverSource.HUMAN
    await repo.upsert(record)
    mission.status = MissionStatus.EXECUTING
    mission.execution.state = ExecutionState.QUEUED
    await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    consumed = await repo.get(waiting.approval_id)
    assert consumed is not None
    assert consumed.status == ApprovalStatus.CONSUMED
    verified = [item for item in mission.actions if item.action_type == ACTION_REMOVE_DUPLICATES]
    assert verified
    assert all(item.status == ActionStatus.VERIFIED for item in verified)
    assert await storage.load("source.csv") == raw
    record.status = ApprovalStatus.APPROVED
    await repo.upsert(record)
    action_count = len(mission.actions)
    await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    assert len(mission.actions) == action_count
    assert mission.model_call_count == model_calls
    assert mission.reasoning_iteration >= iteration


@pytest.mark.asyncio
async def test_reject_does_not_execute_and_replans(tmp_path: Path) -> None:
    repo = SQLiteApprovalRepository(tmp_path / "atlas.db")
    mission, storage, raw, supervisor, waiting, frame = await _run_until_pause(
        tmp_path, repo, PlannerSource.LOCAL_FALLBACK
    )
    record = await repo.get(waiting.approval_id)
    assert record is not None
    record.status = ApprovalStatus.REJECTED
    record.rejection_reason = "not now"
    record.resolver_source = ApprovalResolverSource.HUMAN
    await repo.upsert(record)
    mission.status = MissionStatus.EXECUTING
    mission.execution.state = ExecutionState.QUEUED
    await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    assert all(item.status != ActionStatus.VERIFIED for item in mission.actions)
    assert await storage.load("source.csv") == raw
    assert mission.pending_approval_id is None


@pytest.mark.asyncio
async def test_expired_approval_cannot_execute(tmp_path: Path) -> None:
    repo = SQLiteApprovalRepository(tmp_path / "atlas.db")
    mission, storage, raw, supervisor, waiting, frame = await _run_until_pause(
        tmp_path, repo, PlannerSource.LOCAL_FALLBACK
    )
    record = await repo.get(waiting.approval_id)
    assert record is not None
    record.expires_at = utc_now() - timedelta(seconds=5)
    await repo.upsert(record)
    mission.status = MissionStatus.EXECUTING
    mission.execution.state = ExecutionState.QUEUED
    await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    loaded = await repo.get(waiting.approval_id)
    assert loaded is not None
    assert loaded.status == ApprovalStatus.EXPIRED
    assert all(item.status != ActionStatus.VERIFIED for item in mission.actions)


@pytest.mark.asyncio
async def test_changed_fingerprint_requires_new_approval(tmp_path: Path) -> None:
    repo = SQLiteApprovalRepository(tmp_path / "atlas.db")
    mission, storage, raw, supervisor, waiting, frame = await _run_until_pause(
        tmp_path, repo, PlannerSource.LOCAL_FALLBACK
    )
    record = await repo.get(waiting.approval_id)
    assert record is not None
    record.status = ApprovalStatus.APPROVED
    record.decision_snapshot = {
        "decision": "ACTION",
        "reason": "fill instead",
        "action": {
            "type": ACTION_FILL_MISSING_VALUES,
            "parameters": {"column_name": "customer_age", "strategy": "auto"},
        },
    }
    await repo.upsert(record)
    mission.status = MissionStatus.EXECUTING
    mission.execution.state = ExecutionState.QUEUED
    await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    assert all(item.status != ActionStatus.VERIFIED for item in mission.actions)


@pytest.mark.asyncio
async def test_waiting_mission_is_not_claimable(tmp_path: Path, test_env: Path) -> None:
    missions = SQLiteMissionRepository(test_env)
    approvals = SQLiteApprovalRepository(test_env)
    mission = Mission(goal="fix quality", dataset_id="ds")
    mission.status = MissionStatus.WAITING_FOR_APPROVAL
    mission.execution.state = ExecutionState.WAITING_FOR_APPROVAL
    mission.pending_approval_id = "appr-1"
    await missions.create(mission)
    await approvals.upsert(
        ApprovalRequest(
            approval_id="appr-1",
            mission_id=mission.mission_id,
            requested_operation="ACTION:REMOVE_DUPLICATES",
            operation_kind=ModelDecisionKind.ACTION,
            capability=ACTION_REMOVE_DUPLICATES,
            fingerprint=approval_fingerprint(mission.mission_id, "fp"),
            expires_at=utc_now() + timedelta(hours=1),
        )
    )
    claimed = await missions.claim(mission.mission_id, "worker-a", lease_seconds=30)
    assert claimed is None
    loaded = await missions.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.WAITING_FOR_APPROVAL
    assert len(await approvals.list_for_mission(mission.mission_id)) == 1


@pytest.mark.asyncio
async def test_approval_api_idempotent_and_guards(client: AsyncClient) -> None:
    upload = await client.post(
        "/datasets",
        files={
            "file": (
                "remediation_quality.csv",
                (FIXTURES_DIR / "remediation_quality.csv").read_bytes(),
                "text/csv",
            )
        },
    )
    created = await client.post(
        "/missions",
        json={
            "goal": "Investigate this CSV and fix the major data-quality problems.",
            "dataset_id": upload.json()["dataset_id"],
        },
    )
    mission_id = created.json()["mission_id"]
    waiting = await wait_for_mission_status(
        client, mission_id, {"WAITING_FOR_APPROVAL", "FAILED"}
    )
    assert waiting["status"] == "WAITING_FOR_APPROVAL"
    budgets = (
        waiting["reasoning_trace"][-1]["iteration"] if waiting["reasoning_trace"] else 0,
    )
    again = await client.get(f"/missions/{mission_id}")
    assert again.json()["status"] == "WAITING_FOR_APPROVAL"
    if again.json()["reasoning_trace"]:
        assert again.json()["reasoning_trace"][-1]["iteration"] == budgets[0]
    approval_id = waiting["pending_approval"]["approval_id"]
    first = await client.post(
        f"/missions/{mission_id}/approvals/{approval_id}/approve",
        json={"resolver": "reviewer"},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/missions/{mission_id}/approvals/{approval_id}/approve",
        json={"resolver": "reviewer"},
    )
    assert second.status_code == 200
    missing = await client.post(
        f"/missions/{mission_id}/approvals/does-not-exist/approve",
        json={"resolver": "reviewer"},
    )
    assert missing.status_code == 404
    other = await client.post(
        "/missions",
        json={"goal": "Investigate this CSV only.", "dataset_id": upload.json()["dataset_id"]},
    )
    wrong = await client.post(
        f"/missions/{other.json()['mission_id']}/approvals/{approval_id}/approve",
        json={"resolver": "reviewer"},
    )
    assert wrong.status_code == 404
    rejected_create = await client.post(
        "/missions",
        json={
            "goal": "Investigate this CSV and fix the major data-quality problems.",
            "dataset_id": upload.json()["dataset_id"],
        },
    )
    reject_mission = rejected_create.json()["mission_id"]
    reject_wait = await wait_for_mission_status(
        client, reject_mission, {"WAITING_FOR_APPROVAL", "FAILED"}
    )
    reject_id = reject_wait["pending_approval"]["approval_id"]
    denied = await client.post(
        f"/missions/{reject_mission}/approvals/{reject_id}/reject",
        json={"resolver": "reviewer"},
    )
    assert denied.status_code == 200
    again_reject = await client.post(
        f"/missions/{reject_mission}/approvals/{reject_id}/reject",
        json={"resolver": "reviewer"},
    )
    assert again_reject.status_code == 200
    approve_rejected = await client.post(
        f"/missions/{reject_mission}/approvals/{reject_id}/approve",
        json={"resolver": "reviewer"},
    )
    assert approve_rejected.status_code == 409
    finished: dict = {}
    for _ in range(6):
        payload = await wait_for_mission_status(
            client, reject_mission, {"COMPLETED", "FAILED", "WAITING_FOR_APPROVAL"}
        )
        if payload["status"] in {"COMPLETED", "FAILED"}:
            finished = payload
            break
        listed = await client.get(f"/missions/{reject_mission}/approvals")
        pending = [item for item in listed.json()["items"] if item["status"] == "PENDING"]
        assert pending
        assert pending[0]["approval_id"] != reject_id
        denied_next = await client.post(
            f"/missions/{reject_mission}/approvals/{pending[0]['approval_id']}/reject",
            json={"resolver": "reviewer"},
        )
        assert denied_next.status_code == 200
    else:
        raise TimeoutError("Rejected mission did not finish replanning")
    assert finished["status"] == "COMPLETED"
    assert not any(item["status"] == "VERIFIED" for item in finished.get("actions") or [])


@pytest.mark.asyncio
async def test_approval_service_expired_conflict(tmp_path: Path, test_env: Path) -> None:
    from atlas.execution.recovery import MissionRecoveryService
    from atlas.services.approval_service import ApprovalService

    missions = SQLiteMissionRepository(test_env)
    approvals = SQLiteApprovalRepository(test_env)

    class _Dispatcher:
        backend_name = "local_async"

        async def dispatch(self, mission_id: str) -> None:
            return None

    mission = Mission(goal="fix quality", dataset_id="ds")
    mission.status = MissionStatus.WAITING_FOR_APPROVAL
    mission.execution.state = ExecutionState.WAITING_FOR_APPROVAL
    await missions.create(mission)
    record = ApprovalRequest(
        mission_id=mission.mission_id,
        requested_operation="ACTION:REMOVE_DUPLICATES",
        operation_kind=ModelDecisionKind.ACTION,
        capability=ACTION_REMOVE_DUPLICATES,
        fingerprint=approval_fingerprint(mission.mission_id, "fp"),
        expires_at=utc_now() - timedelta(seconds=1),
    )
    stored = await approvals.upsert(record)
    service = ApprovalService(approvals, missions, _Dispatcher())
    with pytest.raises(ApprovalConflictError):
        await service.approve(mission.mission_id, stored.approval_id, resolver="human")
    with pytest.raises(ApprovalNotFoundError):
        await service.approve("other-mission", stored.approval_id, resolver="human")
    recovery = MissionRecoveryService(missions, _Dispatcher(), approval_repository=approvals)
    await recovery.recover()
    loaded = await approvals.get(stored.approval_id)
    assert loaded is not None
    assert loaded.status == ApprovalStatus.EXPIRED
