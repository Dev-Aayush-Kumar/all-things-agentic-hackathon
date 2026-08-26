"""Typed model decisions and the model-driven supervisor loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.tools import (
    ANALYZE_DUPLICATES,
    INSPECT_COLUMN,
    PROFILE_DATASET,
    ToolContext,
    invoke_tool,
)
from atlas.config.settings import Settings
from atlas.domain.enums import (
    ActionStatus,
    EventType,
    ModelDecisionKind,
    PlannerSource,
    StepStatus,
)
from atlas.domain.exceptions import ModelDecisionError
from atlas.domain.models import (
    Mission,
    ModelDecision,
    ProposedActionRequest,
    ProposedObservation,
    ProposedTask,
    WorkingCopyState,
    public_decision,
)
from atlas.investigation.parser import parse_csv_bytes
from atlas.ops.actions.registry import ACTION_REMOVE_DUPLICATES
from atlas.ops.decisions import parse_model_decision, validate_decision
from atlas.ops.reasoning_context import build_reasoning_context
from atlas.ops.registry import default_registry
from atlas.ops.supervisor import Supervisor
from atlas.ops.workspace import MissionWorkspace
from atlas.storage.local_storage import LocalFileStorage
from tests.conftest import FIXTURES_DIR


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, planner_backend="local", **kwargs)


def _frame(name: str):
    return parse_csv_bytes((FIXTURES_DIR / name).read_bytes())


async def _noop() -> None:
    return None


class ScriptedDecisionMaker:
    """Deterministic fake Gemini. Never calls a paid API."""

    def __init__(
        self,
        decisions: list,
        *,
        source: PlannerSource = PlannerSource.GEMINI_ADK,
        drives_initial_plan: bool = True,
    ) -> None:
        self._decisions = list(decisions)
        self._source = source
        self.drives_initial_plan = drives_initial_plan
        self.seen_contexts: list[dict] = []

    @property
    def source(self) -> PlannerSource:
        return self._source

    async def decide(self, context: dict) -> ModelDecision:
        self.seen_contexts.append(
            {key: value for key, value in context.items() if key != "_workspace"}
        )
        if not self._decisions:
            return ModelDecision(
                decision=ModelDecisionKind.COMPLETE,
                reason="Script exhausted",
                summary="complete",
            )
        item = self._decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return parse_model_decision(item)
        return item


def _workspace(mission: Mission, csv_name: str = "remediation_quality.csv") -> MissionWorkspace:
    return MissionWorkspace(
        mission=mission,
        tool_context=ToolContext("ds", csv_name, _frame(csv_name)),
        persist=_noop,
        lock=__import__("asyncio").Lock(),
        settings=_settings(),
        reasoner=LocalFallbackReasoner(),
        registry=default_registry(),
        plan_source=PlannerSource.GEMINI_ADK,
    )


def test_valid_gemini_style_decision_is_accepted() -> None:
    decision = parse_model_decision(
        {
            "decision": "DELEGATE",
            "reason": "Need a profile",
            "tasks": [{"capability": "DATA_ANALYST", "objective": "Profile the CSV"}],
        }
    )
    mission = Mission(goal="Investigate this CSV", dataset_id="ds")
    validated = validate_decision(decision, _workspace(mission))
    assert validated.decision.decision == ModelDecisionKind.DELEGATE
    assert validated.follow_ups[0].capability == PROFILE_DATASET


def test_malformed_decision_is_rejected() -> None:
    with pytest.raises(ModelDecisionError, match="missing the 'decision' field"):
        parse_model_decision({"reason": "no kind"})
    with pytest.raises(ModelDecisionError, match="JSON object"):
        parse_model_decision("please run a shell")
    with pytest.raises(ModelDecisionError, match="COMPLETE cannot include"):
        parse_model_decision(
            {
                "decision": "COMPLETE",
                "reason": "done",
                "tasks": [{"capability": "profile_dataset"}],
            }
        )


def test_unknown_and_forbidden_capabilities_are_rejected() -> None:
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission)
    with pytest.raises(ModelDecisionError, match="forbidden"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "DELEGATE",
                    "reason": "hack",
                    "tasks": [{"capability": "EXECUTE_SHELL"}],
                }
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError, match="forbidden"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "DELEGATE",
                    "reason": "hack",
                    "tasks": [{"capability": "UNKNOWN_AGENT"}],
                }
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError, match="forbidden"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "ACTION",
                    "reason": "hack",
                    "action": {"type": "DIRECT_FILE_WRITE", "parameters": {}},
                }
            ),
            workspace,
        )


def test_invalid_specialist_and_action_are_rejected() -> None:
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission)
    with pytest.raises(ModelDecisionError, match="Unknown capability"):
        validate_decision(
            ModelDecision(
                decision=ModelDecisionKind.DELEGATE,
                reason="nope",
                tasks=[ProposedTask(capability="launch_missiles")],
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError, match="rejected"):
        validate_decision(
            ModelDecision(
                decision=ModelDecisionKind.ACTION,
                reason="bad params",
                action=ProposedActionRequest(
                    type=ACTION_REMOVE_DUPLICATES,
                    parameters={"shell": "rm -rf /"},
                ),
            ),
            workspace,
        )


def test_gemini_cannot_bypass_registries() -> None:
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission)
    with pytest.raises(ModelDecisionError):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "OBSERVE",
                    "reason": "read secrets",
                    "tool": {"name": "read_env", "arguments": {}},
                }
            ),
            workspace,
        )
    with pytest.raises((Exception, ModelDecisionError)):
        invoke_tool(workspace.tool_context, "EXECUTE_SHELL")


@pytest.mark.asyncio
async def test_scripted_loop_executes_delegate_observe_action_complete(
    tmp_path: Path,
) -> None:
    raw = (FIXTURES_DIR / "remediation_quality.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    frame = parse_csv_bytes(raw)
    column = str(frame.columns[0])
    decider = ScriptedDecisionMaker(
        [
            {
                "decision": "DELEGATE",
                "reason": "Measure the dataset",
                "tasks": [
                    {"capability": "profile_dataset", "objective": "Profile"},
                    {"capability": "analyze_duplicates", "objective": "Duplicates"},
                ],
            },
            {
                "decision": "OBSERVE",
                "reason": "Inspect a column after evidence",
                "tool": {"name": "inspect_column", "arguments": {"column_name": column}},
            },
            {
                "decision": "ACTION",
                "reason": "Duplicates are material",
                "action": {"type": "REMOVE_DUPLICATES", "parameters": {}},
            },
            {
                "decision": "OBSERVE",
                "reason": "Re-check duplicates after remediation",
                "tool": {"name": "analyze_duplicates", "arguments": {}},
            },
            {
                "decision": "COMPLETE",
                "reason": "Goal can be answered",
                "summary": "Duplicates were removed and verified",
            },
        ]
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
        plan_source=PlannerSource.GEMINI_ADK,
        dataset_storage=storage,
        decision_maker=decider,
    )
    await supervisor.run(mission, ToolContext("ds", "remediation_quality.csv", frame), _noop)
    kinds = [
        record.decision.decision
        for record in mission.reasoning_trace
        if record.decision is not None and record.accepted
    ]
    assert ModelDecisionKind.DELEGATE in kinds
    assert ModelDecisionKind.OBSERVE in kinds
    assert ModelDecisionKind.ACTION in kinds
    assert ModelDecisionKind.COMPLETE in kinds
    assert any(item.action_type == ACTION_REMOVE_DUPLICATES for item in mission.actions)
    assert all(
        item.status == ActionStatus.VERIFIED
        for item in mission.actions
        if item.action_type == ACTION_REMOVE_DUPLICATES
    )
    assert await storage.load("source.csv") == raw
    assert mission.working_copy.current_version >= 1
    assert any(record.tool_name == INSPECT_COLUMN for record in mission.evidence_records)
    assert any(record.tool_name == ANALYZE_DUPLICATES for record in mission.evidence_records)
    events = [event.type for event in mission.events]
    assert EventType.MODEL_REASONING_STARTED in events
    assert EventType.MODEL_DECISION_VALIDATED in events
    assert EventType.ACTION_VERIFIED in events
    assert EventType.MODEL_COMPLETED in events
    assert mission.investigation_report is not None
    assert mission.reasoning_trace
    assert all(record.source == PlannerSource.GEMINI_ADK for record in mission.reasoning_trace)
    assert any(
        "findings" in ctx and "allowed_capabilities" in ctx for ctx in decider.seen_contexts
    )
    later = next(
        ctx
        for ctx in decider.seen_contexts
        if ctx.get("actions") and any(item.get("verification_passed") for item in ctx["actions"])
    )
    assert later["actions"]


@pytest.mark.asyncio
async def test_malicious_model_sequence_cannot_execute(tmp_path: Path) -> None:
    raw = (FIXTURES_DIR / "clean_numeric.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    decider = ScriptedDecisionMaker(
        [
            {
                "decision": "DELEGATE",
                "reason": "need profile first",
                "tasks": [{"capability": "profile_dataset"}],
            },
            {
                "decision": "DELEGATE",
                "reason": "shell",
                "tasks": [{"capability": "EXECUTE_SHELL"}],
            },
            {
                "decision": "DELEGATE",
                "reason": "unknown",
                "tasks": [{"capability": "UNKNOWN_AGENT"}],
            },
            {
                "decision": "ACTION",
                "reason": "write",
                "action": {"type": "DIRECT_FILE_WRITE", "parameters": {}},
            },
            {
                "decision": "COMPLETE",
                "reason": "stop after rejections",
                "summary": "safe stop",
            },
        ]
    )
    mission = Mission(goal="Analyze this numeric CSV.", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="clean_numeric.csv",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.GEMINI_ADK,
        dataset_storage=storage,
        decision_maker=decider,
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "clean_numeric.csv", parse_csv_bytes(raw)),
        _noop,
    )
    rejected = [record for record in mission.reasoning_trace if not record.accepted]
    assert len(rejected) >= 3
    assert not any(item.action_type == "DIRECT_FILE_WRITE" for item in mission.actions)
    assert all(task.capability != "EXECUTE_SHELL" for task in (mission.delegation_plan.tasks if mission.delegation_plan else []))
    assert await storage.load("source.csv") == raw
    assert EventType.MODEL_DECISION_REJECTED in {event.type for event in mission.events}


@pytest.mark.asyncio
async def test_model_failure_falls_back_without_mislabelling(tmp_path: Path) -> None:
    raw = (FIXTURES_DIR / "clean_numeric.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    decider = ScriptedDecisionMaker(
        [RuntimeError("adk timeout key=AIzaSyFakeTestKeyValueNotReal00")]
    )
    mission = Mission(goal="Analyze quality problems in this dataset.", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="clean_numeric.csv",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.GEMINI_ADK,
        dataset_storage=storage,
        decision_maker=decider,
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "clean_numeric.csv", parse_csv_bytes(raw)),
        _noop,
    )
    sources = {record.source for record in mission.reasoning_trace}
    assert PlannerSource.GEMINI_ADK in sources
    assert PlannerSource.LOCAL_FALLBACK in sources
    failed = next(record for record in mission.reasoning_trace if not record.accepted)
    assert failed.source == PlannerSource.GEMINI_ADK
    accepted_local = [
        record
        for record in mission.reasoning_trace
        if record.accepted and record.source == PlannerSource.LOCAL_FALLBACK
    ]
    assert accepted_local
    assert mission.investigation_report is not None
    assert mission.investigation_report.reasoning_source == PlannerSource.LOCAL_FALLBACK
    event_types = {event.type for event in mission.events}
    assert EventType.MODEL_DECISION_FALLBACK in event_types
    fallback = next(
        event for event in mission.events if event.type == EventType.MODEL_DECISION_FALLBACK
    )
    assert fallback.metadata["failure_category"] == "timeout"
    assert fallback.metadata["fallback_source"] == PlannerSource.LOCAL_FALLBACK.value
    rejected = next(event for event in mission.events if event.type == EventType.MODEL_DECISION_REJECTED)
    assert "AIzaSyFakeTestKeyValueNotReal00" not in (failed.rejection_reason or "")
    assert "AIzaSyFakeTestKeyValueNotReal00" not in str(rejected.metadata)
    assert "[REDACTED]" in (rejected.metadata.get("error") or "")


@pytest.mark.asyncio
async def test_repeated_identical_decisions_are_bounded() -> None:
    same = {
        "decision": "DELEGATE",
        "reason": "again",
        "tasks": [{"capability": "profile_dataset"}],
    }
    decider = ScriptedDecisionMaker([same, same, same, same])
    mission = Mission(goal="Analyze this CSV", dataset_id="ds")
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(max_repeated_decisions=2, agent_max_iterations=8),
        plan_source=PlannerSource.GEMINI_ADK,
        decision_maker=decider,
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "clean_numeric.csv", _frame("clean_numeric.csv")),
        _noop,
    )
    assert EventType.AGENT_LOOP_LIMIT_REACHED in {event.type for event in mission.events}
    fingerprints = [record.fingerprint for record in mission.reasoning_trace if record.fingerprint]
    assert fingerprints


@pytest.mark.asyncio
async def test_reasoning_loop_respects_max_iterations() -> None:
    decider = ScriptedDecisionMaker(
        [
            {
                "decision": "DELEGATE",
                "reason": "profile",
                "tasks": [{"capability": "profile_dataset"}],
            }
        ]
        + [
            {
                "decision": "OBSERVE",
                "reason": "again",
                "tool": {"name": "analyze_duplicates", "arguments": {}},
            }
        ]
        * 6
    )
    mission = Mission(goal="Analyze this CSV", dataset_id="ds")
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(agent_max_iterations=3),
        plan_source=PlannerSource.GEMINI_ADK,
        decision_maker=decider,
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "clean_numeric.csv", _frame("clean_numeric.csv")),
        _noop,
    )
    assert EventType.AGENT_LOOP_LIMIT_REACHED in {event.type for event in mission.events}


@pytest.mark.asyncio
async def test_local_fallback_still_completes_and_is_labeled() -> None:
    mission = Mission(goal="Analyze quality problems in this dataset.", dataset_id="ds")
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
        decision_maker=LocalDecisionMaker(),
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "survey_quality.csv", _frame("survey_quality.csv")),
        _noop,
    )
    assert mission.investigation_report is not None
    assert mission.investigation_report.reasoning_source == PlannerSource.LOCAL_FALLBACK
    assert mission.reasoning_trace
    assert all(record.source == PlannerSource.LOCAL_FALLBACK for record in mission.reasoning_trace)
    public = [public_decision(item).model_dump() for item in mission.reasoning_trace]
    assert all("idempotency_key" not in item for item in public)
    assert EventType.MODEL_DECISION_VALIDATED in {event.type for event in mission.events}


@pytest.mark.asyncio
async def test_decision_and_evidence_are_persisted_on_the_mission() -> None:
    mission = Mission(goal="Analyze quality problems in this dataset.", dataset_id="ds")
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "survey_quality.csv", _frame("survey_quality.csv")),
        _noop,
    )
    assert mission.reasoning_iteration >= 1
    assert mission.reasoning_trace
    assert mission.evidence_records
    dumped = mission.model_dump()
    assert dumped["reasoning_trace"]
    from atlas.domain.models import MissionDetailResponse

    detail = MissionDetailResponse.from_mission(mission)
    assert detail.reasoning_trace
    assert detail.reasoning_trace[0].source == PlannerSource.LOCAL_FALLBACK
