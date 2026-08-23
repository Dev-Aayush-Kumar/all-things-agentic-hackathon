"""Controlled action layer, working copies, verification, and supervisor ACT loop."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from httpx import AsyncClient

from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.tools import PROFILE_DATASET, ToolContext, ToolSecurityError, invoke_tool
from atlas.config.settings import Settings
from atlas.domain.enums import ActionStatus, EventType, PlannerSource, StepStatus
from atlas.domain.exceptions import (
    ActionAuthorizationError,
    ActionExecutionError,
    ActionValidationError,
    UnknownActionError,
)
from atlas.domain.models import (
    ActionRecord,
    ActionVerification,
    Mission,
    WorkingCopyState,
)
from atlas.investigation.parser import parse_csv_bytes
from atlas.ops.actions.executor import ActionContext, ActionExecutor
from atlas.ops.actions.policy import goal_requests_remediation, propose_action_follow_ups
from atlas.ops.actions.registry import (
    ACTION_FILL_MISSING_VALUES,
    ACTION_REMOVE_DUPLICATES,
    ActionRegistry,
    ActionSpec,
    default_action_registry,
    make_idempotency_key,
)
from atlas.ops.actions.remediations import (
    measure_duplicates,
    transform_fill_missing,
    transform_remove_duplicates,
    verify_fill_missing,
    verify_remove_duplicates,
)
from atlas.ops.registry import DATA_ANALYST_ID, INVESTIGATOR_ID, REMEDIATOR_ID, default_registry
from atlas.ops.supervisor import Supervisor
from atlas.ops.workspace import MissionWorkspace
from atlas.storage.local_storage import LocalFileStorage
from tests.conftest import FIXTURES_DIR, wait_for_mission_status


def _settings(**kwargs) -> Settings:
    values = {"planner_backend": "local", **kwargs}
    return Settings(_env_file=None, **values)


def _frame(name: str) -> pd.DataFrame:
    return parse_csv_bytes((FIXTURES_DIR / name).read_bytes())


def _source_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


async def _noop() -> None:
    return None


def _record(
    mission: Mission,
    action_type: str,
    parameters: dict | None = None,
    *,
    agent_id: str = REMEDIATOR_ID,
    input_version: int = 0,
) -> ActionRecord:
    parameters = parameters or {}
    return ActionRecord(
        mission_id=mission.mission_id,
        agent_id=agent_id,
        action_type=action_type,
        objective=f"Execute {action_type}",
        parameters=parameters,
        status=ActionStatus.PROPOSED,
        provenance=PlannerSource.LOCAL_FALLBACK,
        idempotency_key=make_idempotency_key(
            mission_id=mission.mission_id,
            action_type=action_type,
            parameters=parameters,
            input_version=input_version,
        ),
        input_version=input_version,
    )


def _context(mission: Mission, frame: pd.DataFrame, storage: LocalFileStorage) -> ActionContext:
    return ActionContext(
        mission=mission,
        agent_id=REMEDIATOR_ID,
        storage=storage,
        frame=frame,
        persist=_noop,
    )


async def _run_supervisor_with_storage(
    goal: str,
    csv_name: str,
    tmp_path: Path,
    settings: Settings | None = None,
) -> tuple[Mission, LocalFileStorage, bytes]:
    settings = settings or _settings()
    raw = _source_bytes(csv_name)
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission = Mission(goal=goal, dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename=csv_name,
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        dataset_storage=storage,
    )
    await supervisor.run(mission, ToolContext("ds", csv_name, parse_csv_bytes(raw)), _noop)
    return mission, storage, raw


def test_action_registry_lists_required_remediations() -> None:
    registry = default_action_registry()
    types = {item.action_type for item in registry.all()}
    assert ACTION_REMOVE_DUPLICATES in types
    assert ACTION_FILL_MISSING_VALUES in types
    spec = registry.get(ACTION_REMOVE_DUPLICATES)
    assert REMEDIATOR_ID in spec.allowed_agents
    assert spec.to_descriptor().risk.value == "LOW"


def test_unknown_actions_are_rejected() -> None:
    with pytest.raises(UnknownActionError, match="DROP_TABLE"):
        default_action_registry().get("DROP_TABLE")


def test_unauthorized_agents_are_rejected() -> None:
    registry = default_action_registry()
    with pytest.raises(ActionAuthorizationError, match=DATA_ANALYST_ID):
        registry.authorize(ACTION_REMOVE_DUPLICATES, DATA_ANALYST_ID)
    with pytest.raises(ActionAuthorizationError, match=INVESTIGATOR_ID):
        registry.authorize(ACTION_FILL_MISSING_VALUES, INVESTIGATOR_ID)
    registry.authorize(ACTION_REMOVE_DUPLICATES, REMEDIATOR_ID)


def test_invalid_parameters_are_rejected() -> None:
    registry = default_action_registry()
    with pytest.raises(ActionValidationError, match="missing required"):
        registry.validate_parameters(ACTION_FILL_MISSING_VALUES, {})
    with pytest.raises(ActionValidationError, match="unknown parameters"):
        registry.validate_parameters(ACTION_REMOVE_DUPLICATES, {"shell": "rm -rf /"})
    with pytest.raises(ActionValidationError, match="unknown parameters"):
        registry.validate_parameters(
            ACTION_FILL_MISSING_VALUES,
            {"column_name": "age", "eval": "os.system('id')"},
        )


def test_observation_tools_cannot_execute_actions() -> None:
    frame = _frame("remediation_quality.csv")
    with pytest.raises(ToolSecurityError, match="not an allowed"):
        invoke_tool(ToolContext("ds", "x.csv", frame), ACTION_REMOVE_DUPLICATES)
    default_registry().authorize_tool(DATA_ANALYST_ID, PROFILE_DATASET)
    with pytest.raises(PermissionError):
        default_registry().authorize_tool(REMEDIATOR_ID, PROFILE_DATASET)


def test_goal_policy_does_not_treat_recommendations_as_actions() -> None:
    assert goal_requests_remediation(
        "Investigate this CSV and fix the major data-quality problems."
    )
    assert not goal_requests_remediation(
        "Analyze this survey dataset and tell me what should be fixed first."
    )
    assert not goal_requests_remediation("Check only for duplicate rows in this CSV.")


@pytest.mark.asyncio
async def test_two_remediations_modify_working_copy_not_source(tmp_path: Path) -> None:
    raw = _source_bytes("remediation_quality.csv")
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    original = parse_csv_bytes(raw)
    mission = Mission(goal="fix the quality issues", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="remediation_quality.csv",
    )
    executor = ActionExecutor()
    first = _record(mission, ACTION_REMOVE_DUPLICATES)
    mission.actions.append(first)
    first = await executor.execute(first, _context(mission, original.copy(), storage))
    assert first.status == ActionStatus.VERIFIED
    assert first.verification is not None
    assert first.verification.passed is True
    assert first.verification.after["duplicate_count"] == 0
    assert mission.working_copy.current_version == 1

    working = parse_csv_bytes(await storage.load(mission.working_copy.current_filename()))
    second = _record(
        mission,
        ACTION_FILL_MISSING_VALUES,
        {"column_name": "customer_age", "strategy": "auto"},
        input_version=1,
    )
    mission.actions.append(second)
    second = await executor.execute(second, _context(mission, working, storage))
    assert second.status == ActionStatus.VERIFIED
    assert second.verification is not None
    assert second.verification.after["missing_count"] == 0
    assert mission.working_copy.current_version == 2
    assert [item.version for item in mission.working_copy.versions] == [1, 2]

    source_after = await storage.load("source.csv")
    assert source_after == raw
    latest = parse_csv_bytes(await storage.load(mission.working_copy.current_filename()))
    assert int(latest.duplicated().sum()) == 0
    assert int(latest["customer_age"].isna().sum()) == 0
    assert len(latest) == first.verification.after["row_count"]


def test_verification_is_mandatory_and_can_fail() -> None:
    frame = _frame("remediation_quality.csv")
    before = measure_duplicates(frame)
    after = transform_remove_duplicates(frame, {})
    ok = verify_remove_duplicates(before, after, {})
    assert ok.passed is True
    failed = verify_remove_duplicates(before, frame, {})
    assert failed.passed is False
    missing_before = {"row_count": len(frame), "missing_count": 3, "column_name": "customer_age"}
    filled = transform_fill_missing(frame, {"column_name": "customer_age", "strategy": "auto"})
    filled_ok = verify_fill_missing(missing_before, filled, {"column_name": "customer_age"})
    assert filled_ok.passed is True
    not_filled = verify_fill_missing(missing_before, frame, {"column_name": "customer_age"})
    assert not_filled.passed is False


@pytest.mark.asyncio
async def test_verification_failure_does_not_advance_working_copy(tmp_path: Path) -> None:
    raw = _source_bytes("remediation_quality.csv")
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission = Mission(goal="fix", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="remediation_quality.csv",
    )

    def always_fail(before, after, parameters) -> ActionVerification:
        return ActionVerification(
            passed=False,
            before=before,
            after={"duplicate_count": 99},
            expected={"duplicate_count": 0},
            actual={"duplicate_count": 99},
            summary="forced verification failure",
        )

    registry = ActionRegistry(
        [
            ActionSpec(
                action_type=ACTION_REMOVE_DUPLICATES,
                description="test",
                allowed_agents=frozenset({REMEDIATOR_ID}),
                measure=measure_duplicates,
                transform=transform_remove_duplicates,
                verify=always_fail,
            )
        ]
    )
    executor = ActionExecutor(registry)
    record = _record(mission, ACTION_REMOVE_DUPLICATES)
    mission.actions.append(record)
    with pytest.raises(ActionExecutionError, match="forced verification failure"):
        await executor.execute(record, _context(mission, parse_csv_bytes(raw), storage))
    assert record.status == ActionStatus.VERIFICATION_FAILED
    assert mission.working_copy.current_version == 0
    assert await storage.load("source.csv") == raw
    assert record.status != ActionStatus.VERIFIED
    assert record.status != ActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_action_retry_is_bounded(tmp_path: Path) -> None:
    raw = _source_bytes("remediation_quality.csv")
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission = Mission(goal="fix", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="x.csv",
    )

    def always_fail(before, after, parameters) -> ActionVerification:
        return ActionVerification(passed=False, summary="still failing")

    registry = ActionRegistry(
        [
            ActionSpec(
                action_type=ACTION_REMOVE_DUPLICATES,
                description="test",
                allowed_agents=frozenset({REMEDIATOR_ID}),
                measure=measure_duplicates,
                transform=transform_remove_duplicates,
                verify=always_fail,
            )
        ]
    )
    executor = ActionExecutor(registry)
    record = _record(mission, ACTION_REMOVE_DUPLICATES)
    record.max_attempts = 2
    mission.actions.append(record)
    with pytest.raises(ActionExecutionError):
        await executor.execute(record, _context(mission, parse_csv_bytes(raw), storage))
    with pytest.raises(ActionExecutionError):
        await executor.execute(record, _context(mission, parse_csv_bytes(raw), storage))
    assert record.attempt_count == 2
    with pytest.raises(ActionExecutionError, match="exhausted"):
        await executor.execute(record, _context(mission, parse_csv_bytes(raw), storage))
    assert record.attempt_count == 2
    assert record.status == ActionStatus.VERIFICATION_FAILED


@pytest.mark.asyncio
async def test_repeated_action_execution_is_idempotent(tmp_path: Path) -> None:
    raw = _source_bytes("remediation_quality.csv")
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission = Mission(goal="fix", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="x.csv",
    )
    executor = ActionExecutor()
    record = _record(mission, ACTION_REMOVE_DUPLICATES)
    mission.actions.append(record)
    first = await executor.execute(record, _context(mission, parse_csv_bytes(raw), storage))
    version = mission.working_copy.current_version
    filename = mission.working_copy.current_filename()
    second = await executor.execute(first, _context(mission, parse_csv_bytes(raw), storage))
    assert second.action_id == first.action_id
    assert second.status == ActionStatus.VERIFIED
    assert mission.working_copy.current_version == version
    assert mission.working_copy.current_filename() == filename
    assert len(mission.working_copy.versions) == 1


@pytest.mark.asyncio
async def test_completed_action_is_not_rerun_after_recovery(tmp_path: Path) -> None:
    raw = _source_bytes("remediation_quality.csv")
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    mission = Mission(goal="fix", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="x.csv",
    )
    executor = ActionExecutor()
    record = _record(mission, ACTION_REMOVE_DUPLICATES)
    mission.actions.append(record)
    completed = await executor.execute(record, _context(mission, parse_csv_bytes(raw), storage))
    assert completed.status == ActionStatus.VERIFIED
    completed.status = ActionStatus.RUNNING
    from atlas.domain.models import DelegationPlan, SpecialistTask

    mission.delegation_plan = DelegationPlan(
        objective="resume",
        source=PlannerSource.LOCAL_FALLBACK,
        tasks=[
            SpecialistTask(
                task_id="spec_remove_duplicates_1",
                mission_id=mission.mission_id,
                agent_id=REMEDIATOR_ID,
                objective="Remove duplicates",
                capability="remove_duplicates",
                status=StepStatus.COMPLETED,
            )
        ],
    )
    Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
        dataset_storage=storage,
    )._prepare_plan(mission)
    assert completed.status == ActionStatus.PROPOSED
    completed.status = ActionStatus.VERIFIED
    reused = await executor.execute(
        completed, _context(mission, parse_csv_bytes(raw), storage)
    )
    assert reused.status == ActionStatus.VERIFIED
    assert mission.working_copy.current_version == 1


@pytest.mark.asyncio
async def test_supervisor_selects_observes_and_replans_actions(tmp_path: Path) -> None:
    mission, storage, raw = await _run_supervisor_with_storage(
        "Investigate this CSV and fix the major data-quality problems.",
        "remediation_quality.csv",
        tmp_path,
    )
    types = [item.action_type for item in mission.actions]
    assert ACTION_REMOVE_DUPLICATES in types
    assert ACTION_FILL_MISSING_VALUES in types
    assert all(
        item.status == ActionStatus.VERIFIED
        for item in mission.actions
        if item.action_type in {ACTION_REMOVE_DUPLICATES, ACTION_FILL_MISSING_VALUES}
    )
    assert mission.working_copy is not None
    assert mission.working_copy.current_version >= 2
    assert await storage.load("source.csv") == raw
    events = [event.type for event in mission.events]
    assert EventType.ACTION_PROPOSED in events
    assert EventType.ACTION_AUTHORIZED in events
    assert EventType.ACTION_STARTED in events
    assert EventType.ACTION_COMPLETED in events
    assert EventType.ACTION_VERIFIED in events
    assert EventType.WORKING_COPY_CREATED in events
    assert EventType.WORKING_COPY_UPDATED in events
    assert EventType.REPLAN_AFTER_ACTION in events
    assert EventType.SUPERVISOR_OBSERVED in events
    assert mission.investigation_report is not None
    assert mission.investigation_report.reasoning_source == PlannerSource.LOCAL_FALLBACK
    assert mission.investigation_report.actions_performed
    assert mission.investigation_report.working_copy is not None
    remediator_tasks = [
        task
        for task in (mission.delegation_plan.tasks if mission.delegation_plan else [])
        if task.agent_id == REMEDIATOR_ID
    ]
    assert remediator_tasks
    assert all(task.status == StepStatus.COMPLETED for task in remediator_tasks)


@pytest.mark.asyncio
async def test_non_action_missions_still_work(tmp_path: Path) -> None:
    mission, storage, raw = await _run_supervisor_with_storage(
        "Analyze quality problems in this dataset.",
        "survey_quality.csv",
        tmp_path,
    )
    assert mission.actions == []
    assert mission.investigation_report is not None
    assert mission.delegation_plan is not None
    assert not any(task.agent_id == REMEDIATOR_ID for task in mission.delegation_plan.tasks)
    assert await storage.load("source.csv") == raw


@pytest.mark.asyncio
async def test_supervisor_action_policy_uses_evidence(tmp_path: Path) -> None:
    settings = _settings()
    mission = Mission(
        goal="Investigate this CSV and fix the major data-quality problems.",
        dataset_id="ds",
    )
    from atlas.ops.planning import build_initial_delegation

    mission.delegation_plan = build_initial_delegation(
        mission,
        tools=[PROFILE_DATASET],
        source=PlannerSource.LOCAL_FALLBACK,
        registry=default_registry(),
        max_attempts=2,
    )
    workspace = MissionWorkspace(
        mission=mission,
        tool_context=ToolContext("ds", "x.csv", _frame("remediation_quality.csv")),
        persist=_noop,
        lock=__import__("asyncio").Lock(),
        settings=settings,
        reasoner=LocalFallbackReasoner(),
        registry=default_registry(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )
    assert propose_action_follow_ups(workspace) == []
    from atlas.investigation.duplicates import analyze_duplicates
    from atlas.investigation.missing import analyze_missing

    mission.findings = analyze_duplicates(_frame("remediation_quality.csv"))
    proposed = propose_action_follow_ups(workspace)
    assert len(proposed) == 1
    assert proposed[0].arguments["action_type"] == ACTION_REMOVE_DUPLICATES
    mission.actions.append(
        _record(mission, ACTION_REMOVE_DUPLICATES)
    )
    mission.findings = analyze_missing(_frame("remediation_quality.csv"))
    proposed = propose_action_follow_ups(workspace)
    assert proposed
    assert proposed[0].arguments["action_type"] == ACTION_FILL_MISSING_VALUES
    assert proposed[0].arguments["parameters"]["column_name"] == "customer_age"


@pytest.mark.asyncio
async def test_http_mission_exposes_action_state(client: AsyncClient) -> None:
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
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED", "FAILED"}
    )
    assert final["status"] == "COMPLETED"
    assert final["actions"]
    assert final["working_copy"] is not None
    assert final["working_copy"]["current_version"] >= 1
    assert final["investigation_report"]["actions_performed"]
    assert final["investigation_report"]["reasoning_source"] == "LOCAL_FALLBACK"
    event_types = [event["type"] for event in final["events"]]
    assert "ACTION_PROPOSED" in event_types
    assert "ACTION_VERIFIED" in event_types
    assert "WORKING_COPY_CREATED" in event_types
    statuses = {item["status"] for item in final["actions"]}
    assert "VERIFIED" in statuses
    for item in final["actions"]:
        assert "idempotency_key" not in item
        assert item.get("verification_passed") is True
