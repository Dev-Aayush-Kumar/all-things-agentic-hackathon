"""Multi-agent supervisor, registry, delegation, and recovery tests."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.tools import (
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    INSPECT_COLUMN,
    PROFILE_DATASET,
    ToolContext,
    ToolSecurityError,
    invoke_tool,
)
from atlas.config.settings import Settings
from atlas.domain.enums import EventType, PlannerSource, StepStatus
from atlas.domain.models import (
    DelegationPlan,
    Mission,
    SpecialistTask,
    SpecialistTaskResult,
)
from atlas.investigation.parser import parse_csv_bytes
from atlas.ops.delegation import LocalDelegationManager
from atlas.ops.planning import build_initial_delegation, ready_tasks
from atlas.ops.registry import (
    CAPABILITY_SYNTHESIZE,
    DATA_ANALYST_ID,
    INVESTIGATOR_ID,
    REMEDIATOR_ID,
    REPORTER_ID,
    UnknownCapabilityError,
    default_registry,
)
from atlas.ops.specialists import DataAnalystAgent
from atlas.ops.supervisor import CriticalTaskFailedError, Supervisor
from atlas.ops.workspace import MissionWorkspace
from tests.conftest import FIXTURES_DIR, wait_for_mission_status


def _settings(**kwargs) -> Settings:
    values = {"planner_backend": "local", **kwargs}
    return Settings(_env_file=None, **values)


def _frame(name: str):
    return parse_csv_bytes((FIXTURES_DIR / name).read_bytes())


async def _run_supervisor(goal: str, csv_name: str, settings: Settings | None = None) -> Mission:
    settings = settings or _settings()
    mission = Mission(goal=goal, dataset_id="ds")
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )

    async def persist() -> None:
        return None

    await supervisor.run(
        mission,
        ToolContext("ds", csv_name, _frame(csv_name)),
        persist,
    )
    return mission


def test_registry_discovers_specialists() -> None:
    registry = default_registry()
    ids = {item.id for item in registry.all()}
    assert ids == {DATA_ANALYST_ID, INVESTIGATOR_ID, REPORTER_ID, REMEDIATOR_ID}
    analyst = registry.get(DATA_ANALYST_ID)
    assert PROFILE_DATASET in analyst.capabilities
    assert INSPECT_COLUMN in analyst.allowed_tools
    investigator = registry.get(INVESTIGATOR_ID)
    assert INSPECT_COLUMN in investigator.allowed_tools
    assert PROFILE_DATASET not in investigator.allowed_tools
    reporter = registry.get(REPORTER_ID)
    assert reporter.allowed_tools == []
    remediator = registry.get(REMEDIATOR_ID)
    assert remediator.allowed_tools == []
    assert "remove_duplicates" in remediator.capabilities


def test_capability_matching() -> None:
    registry = default_registry()
    assert registry.match(PROFILE_DATASET).id == DATA_ANALYST_ID
    assert registry.match("investigate_evidence").id == INVESTIGATOR_ID
    assert registry.match(CAPABILITY_SYNTHESIZE).id == REPORTER_ID
    with pytest.raises(UnknownCapabilityError):
        registry.match("launch_missiles")


def test_specialist_tool_allowlist_is_enforced() -> None:
    registry = default_registry()
    with pytest.raises(PermissionError, match="not authorized"):
        registry.authorize_tool(INVESTIGATOR_ID, PROFILE_DATASET)
    registry.authorize_tool(INVESTIGATOR_ID, INSPECT_COLUMN)
    with pytest.raises(PermissionError):
        registry.authorize_tool(REPORTER_ID, ANALYZE_MISSING)


def test_supervisor_creates_structural_delegation_plan() -> None:
    mission = Mission(goal="Analyze quality problems in this dataset.", dataset_id="ds")
    tools = [PROFILE_DATASET, ANALYZE_MISSING, ANALYZE_DUPLICATES]
    plan = build_initial_delegation(
        mission,
        tools=tools,
        source=PlannerSource.LOCAL_FALLBACK,
        registry=default_registry(),
        max_attempts=2,
    )
    assert plan.tasks
    assert plan.tasks[0].capability == PROFILE_DATASET
    assert plan.tasks[0].critical is True
    assert plan.tasks[0].agent_id == DATA_ANALYST_ID
    later = [task for task in plan.tasks if task.capability != PROFILE_DATASET]
    assert later
    assert all(task.depends_on == ["spec_profile"] for task in later)
    assert all(task.mission_id == mission.mission_id for task in plan.tasks)
    assert mission.agent_plan is not None
    assert CAPABILITY_SYNTHESIZE not in {task.capability for task in plan.tasks}
    assert "investigate_evidence" not in {task.capability for task in plan.tasks}


def test_dependencies_are_enforced() -> None:
    mission = Mission(goal="quality", dataset_id="ds")
    plan = build_initial_delegation(
        mission,
        tools=[PROFILE_DATASET, ANALYZE_MISSING, ANALYZE_DUPLICATES],
        source=PlannerSource.LOCAL_FALLBACK,
        registry=default_registry(),
        max_attempts=2,
    )
    ready = ready_tasks(plan)
    assert [task.capability for task in ready] == [PROFILE_DATASET]
    plan.tasks[0].status = StepStatus.COMPLETED
    ready = ready_tasks(plan)
    caps = {task.capability for task in ready}
    assert caps == {ANALYZE_MISSING, ANALYZE_DUPLICATES}


@pytest.mark.asyncio
async def test_independent_tasks_execute_concurrently() -> None:
    started: list[float] = []

    class SlowAnalyst:
        def __init__(self, inner: DataAnalystAgent) -> None:
            self.inner = inner
            self.descriptor = inner.descriptor

        async def execute(self, task, workspace):
            started.append(time.monotonic())
            await asyncio.sleep(0.08)
            return SpecialistTaskResult(
                summary="slow-ok",
                provenance=PlannerSource.LOCAL_FALLBACK,
            )

    registry = default_registry()
    inner = DataAnalystAgent(registry.get(DATA_ANALYST_ID))
    manager = LocalDelegationManager({DATA_ANALYST_ID: SlowAnalyst(inner)})
    mission = Mission(goal="quality", dataset_id="ds")
    tasks = [
        SpecialistTask(
            task_id="a",
            mission_id=mission.mission_id,
            agent_id=DATA_ANALYST_ID,
            objective="a",
            capability=ANALYZE_MISSING,
        ),
        SpecialistTask(
            task_id="b",
            mission_id=mission.mission_id,
            agent_id=DATA_ANALYST_ID,
            objective="b",
            capability=ANALYZE_DUPLICATES,
        ),
    ]
    workspace = MissionWorkspace(
        mission=mission,
        tool_context=ToolContext("ds", "x.csv", _frame("clean_numeric.csv")),
        persist=_noop_persist,
        lock=asyncio.Lock(),
        settings=_settings(),
        reasoner=LocalFallbackReasoner(),
        registry=registry,
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )
    began = time.monotonic()
    await manager.execute_ready(tasks, workspace)
    elapsed = time.monotonic() - began
    assert len(started) == 2
    assert abs(started[0] - started[1]) < 0.2
    assert elapsed < 0.5
    assert all(task.status == StepStatus.COMPLETED for task in tasks)


async def _noop_persist() -> None:
    return None


@pytest.mark.asyncio
async def test_agent_results_are_persisted_on_the_mission() -> None:
    mission = await _run_supervisor(
        "Check this dataset for missing values.",
        "missing_heavy.csv",
    )
    assert mission.delegation_plan is not None
    completed = [
        task for task in mission.delegation_plan.tasks if task.status == StepStatus.COMPLETED
    ]
    assert completed
    assert any(task.result is not None for task in completed)
    assert mission.findings
    assert mission.dataset_profile is not None
    assert mission.evidence_records


@pytest.mark.asyncio
async def test_supervisor_observes_and_replans_from_evidence() -> None:
    mission = await _run_supervisor(
        "Check only for duplicate rows in this CSV.",
        "survey_quality.csv",
    )
    event_types = [event.type for event in mission.events]
    assert EventType.DELEGATION_PLAN_CREATED in event_types
    assert EventType.SUPERVISOR_OBSERVED in event_types
    assert EventType.REPLAN_TRIGGERED in event_types
    assert EventType.ADAPTIVE_INVESTIGATION_TRIGGERED in event_types
    adaptive = [
        event
        for event in mission.events
        if event.type == EventType.ADAPTIVE_INVESTIGATION_TRIGGERED
    ]
    assert any(event.metadata.get("tool_name") == "analyze_outliers" for event in adaptive)
    capabilities = [task.capability for task in mission.delegation_plan.tasks]
    assert "analyze_outliers" in capabilities
    created = next(
        event for event in mission.events if event.type == EventType.DELEGATION_PLAN_CREATED
    )
    assert created.metadata["task_count"] >= 2


@pytest.mark.asyncio
async def test_adaptive_delegation_changes_subsequent_work() -> None:
    survey = await _run_supervisor(
        "Check only for duplicate rows in this CSV.",
        "survey_quality.csv",
    )
    clean = await _run_supervisor(
        "Check only for duplicate rows in this CSV.",
        "clean_numeric.csv",
    )
    survey_caps = {task.capability for task in survey.delegation_plan.tasks}
    clean_caps = {task.capability for task in clean.delegation_plan.tasks}
    assert "analyze_outliers" in survey_caps
    assert "analyze_outliers" not in clean_caps
    assert CAPABILITY_SYNTHESIZE in survey_caps
    assert CAPABILITY_SYNTHESIZE in clean_caps
    investigator_caps = {"investigate_evidence", "investigate_column"}
    # Investigator is added only when findings exist, not as a fixed pipeline.
    assert survey_caps & investigator_caps or "analyze_outliers" in survey_caps
    if not clean.findings:
        assert not (clean_caps & investigator_caps)


@pytest.mark.asyncio
async def test_non_critical_failure_allows_mission_to_complete() -> None:
    original = invoke_tool

    def flaky(context, tool_name, **kwargs):
        if tool_name == ANALYZE_DUPLICATES:
            raise ToolSecurityError("injected duplicates failure")
        return original(context, tool_name, **kwargs)

    with patch("atlas.ops.tooling.invoke_tool", side_effect=flaky):
        mission = await _run_supervisor(
            "Check only for duplicate rows in this CSV.",
            "clean_numeric.csv",
            settings=_settings(specialist_task_max_attempts=2),
        )
    dup = next(
        task
        for task in mission.delegation_plan.tasks
        if task.capability == ANALYZE_DUPLICATES
    )
    assert dup.status == StepStatus.FAILED
    assert dup.attempt_count == dup.max_attempts
    assert mission.investigation_report is not None
    assert mission.dataset_profile is not None
    assert any(event.type == EventType.AGENT_FAILED for event in mission.events)
    assert any(event.type == EventType.TASK_SKIPPED for event in mission.events)


@pytest.mark.asyncio
async def test_critical_failure_fails_the_mission() -> None:
    def always_fail(context, tool_name, **kwargs):
        raise ToolSecurityError("injected profile failure")

    with patch("atlas.ops.tooling.invoke_tool", side_effect=always_fail):
        with pytest.raises(CriticalTaskFailedError):
            await _run_supervisor(
                "Analyze quality problems in this dataset.",
                "clean_numeric.csv",
                settings=_settings(specialist_task_max_attempts=2),
            )


@pytest.mark.asyncio
async def test_retry_limits_are_respected() -> None:
    calls = {"n": 0}
    original = invoke_tool

    def flaky(context, tool_name, **kwargs):
        if tool_name == ANALYZE_MISSING:
            calls["n"] += 1
            raise ToolSecurityError("injected missing failure")
        return original(context, tool_name, **kwargs)

    with patch("atlas.ops.tooling.invoke_tool", side_effect=flaky):
        mission = await _run_supervisor(
            "Check this dataset for missing values.",
            "clean_numeric.csv",
            settings=_settings(specialist_task_max_attempts=2),
        )
    missing = next(
        task
        for task in mission.delegation_plan.tasks
        if task.capability == ANALYZE_MISSING
    )
    assert missing.attempt_count == 2
    assert calls["n"] == 2
    assert missing.status == StepStatus.FAILED


@pytest.mark.asyncio
async def test_completed_specialist_tasks_are_not_rerun_on_resume() -> None:
    settings = _settings()
    mission = Mission(
        goal="Check this dataset for missing values.",
        dataset_id="ds",
    )
    plan = build_initial_delegation(
        mission,
        tools=[PROFILE_DATASET, ANALYZE_MISSING],
        source=PlannerSource.LOCAL_FALLBACK,
        registry=default_registry(),
        max_attempts=2,
    )
    profile = plan.tasks[0]
    profile.status = StepStatus.COMPLETED
    profile.result = SpecialistTaskResult(
        summary="cached profile",
        provenance=PlannerSource.LOCAL_FALLBACK,
    )
    mission.delegation_plan = plan
    mission.agent_plan.tasks[0].status = StepStatus.COMPLETED
    mission.agent_plan.tasks[0].result_summary = "cached"
    profile_result = invoke_tool(
        ToolContext("ds", "missing_heavy.csv", _frame("missing_heavy.csv")),
        PROFILE_DATASET,
    )
    mission.dataset_profile = profile_result.profile
    mission.evidence_records = []

    called: list[str] = []
    original = invoke_tool

    def tracking(context, tool_name, **kwargs):
        called.append(tool_name)
        return original(context, tool_name, **kwargs)

    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        selected_tools=[PROFILE_DATASET, ANALYZE_MISSING],
    )
    with patch("atlas.ops.tooling.invoke_tool", side_effect=tracking):
        await supervisor.run(
            mission,
            ToolContext("ds", "missing_heavy.csv", _frame("missing_heavy.csv")),
            _noop_persist,
        )
    assert PROFILE_DATASET not in called
    assert ANALYZE_MISSING in called
    assert mission.investigation_report is not None


@pytest.mark.asyncio
async def test_http_mission_exposes_agent_and_task_state(client: AsyncClient) -> None:
    upload = await client.post(
        "/datasets",
        files={
            "file": (
                "survey_quality.csv",
                (FIXTURES_DIR / "survey_quality.csv").read_bytes(),
                "text/csv",
            )
        },
    )
    created = await client.post(
        "/missions",
        json={
            "goal": (
                "Analyze this survey dataset, identify the most important quality "
                "problems, investigate what may be causing them, and tell me what "
                "should be fixed first."
            ),
            "dataset_id": upload.json()["dataset_id"],
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED", "FAILED"}
    )
    assert final["status"] == "COMPLETED"
    assert final["current_objective"]
    plan = final["delegation_plan"]
    assert plan is not None
    assert plan["tasks"]
    assert {task["agent_id"] for task in plan["tasks"]} >= {DATA_ANALYST_ID, REPORTER_ID}
    assert any(task["status"] == "COMPLETED" for task in plan["tasks"])
    assert any(task["capability"] == PROFILE_DATASET for task in plan["tasks"])
    event_types = [event["type"] for event in final["events"]]
    for expected in [
        "DELEGATION_PLAN_CREATED",
        "TASK_DELEGATED",
        "AGENT_STARTED",
        "AGENT_COMPLETED",
        "SUPERVISOR_OBSERVED",
        "SYNTHESIS_COMPLETED",
    ]:
        assert expected in event_types
    assert final["investigation_report"]["reasoning_source"] == "LOCAL_FALLBACK"


@pytest.mark.asyncio
async def test_local_fallback_label_remains_explicit() -> None:
    mission = await _run_supervisor(
        "Analyze quality problems in this dataset.",
        "survey_quality.csv",
    )
    assert mission.delegation_plan is not None
    assert mission.delegation_plan.source == PlannerSource.LOCAL_FALLBACK
    assert mission.investigation_report is not None
    assert mission.investigation_report.reasoning_source == PlannerSource.LOCAL_FALLBACK
    reporter = next(
        task
        for task in mission.delegation_plan.tasks
        if task.capability == CAPABILITY_SYNTHESIZE
    )
    assert reporter.result is not None
    assert reporter.result.provenance == PlannerSource.LOCAL_FALLBACK
