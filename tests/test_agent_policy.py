"""Deterministic agent policy and loop tests."""

import pytest

from atlas.investigation.parser import parse_csv_bytes

from atlas.agent.loop import AgentLoop
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.policy import decide_adaptive_actions, select_tools
from atlas.agent.tools import (
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    INSPECT_COLUMN,
    PROFILE_DATASET,
    ToolContext,
    invoke_tool,
)
from atlas.config.settings import Settings
from atlas.domain.enums import EventType, PlannerSource, StepStatus
from atlas.domain.models import AgentPlan, AgentTask, Mission
from tests.conftest import FIXTURES_DIR


def test_select_tools_depends_on_goal() -> None:
    duplicates_only = select_tools("Check only for duplicate rows in this CSV.")
    assert PROFILE_DATASET in duplicates_only
    assert ANALYZE_DUPLICATES in duplicates_only
    assert ANALYZE_OUTLIERS not in duplicates_only
    assert ANALYZE_MISSING not in duplicates_only
    assert INSPECT_COLUMN not in duplicates_only

    quality = select_tools(
        "Analyze this survey dataset, identify the most important quality "
        "problems, investigate what may be causing them, and tell me what "
        "should be fixed first."
    )
    assert PROFILE_DATASET in quality
    assert ANALYZE_MISSING in quality
    assert ANALYZE_DUPLICATES in quality
    assert ANALYZE_OUTLIERS in quality
    assert INSPECT_COLUMN not in quality


def test_adaptive_outliers_only_when_profile_is_extreme() -> None:
    survey = parse_csv_bytes((FIXTURES_DIR / "survey_quality.csv").read_bytes())
    clean = parse_csv_bytes((FIXTURES_DIR / "clean_numeric.csv").read_bytes())
    survey_profile = invoke_tool(
        ToolContext("s", "survey.csv", survey), PROFILE_DATASET
    )
    clean_profile = invoke_tool(
        ToolContext("c", "clean.csv", clean), PROFILE_DATASET
    )

    survey_actions = decide_adaptive_actions(
        completed_tools={PROFILE_DATASET},
        results=[survey_profile],
        inspected_columns=set(),
        planned_tools={PROFILE_DATASET, ANALYZE_DUPLICATES},
    )
    clean_actions = decide_adaptive_actions(
        completed_tools={PROFILE_DATASET},
        results=[clean_profile],
        inspected_columns=set(),
        planned_tools={PROFILE_DATASET, ANALYZE_DUPLICATES},
    )
    assert any(action.tool_name == ANALYZE_OUTLIERS for action in survey_actions)
    assert not any(action.tool_name == ANALYZE_OUTLIERS for action in clean_actions)


def test_adaptive_inspect_column_only_when_missing_is_material() -> None:
    heavy = parse_csv_bytes((FIXTURES_DIR / "missing_heavy.csv").read_bytes())
    clean = parse_csv_bytes((FIXTURES_DIR / "clean_numeric.csv").read_bytes())
    heavy_missing = invoke_tool(ToolContext("h", "heavy.csv", heavy), ANALYZE_MISSING)
    clean_missing = invoke_tool(ToolContext("c", "clean.csv", clean), ANALYZE_MISSING)

    heavy_actions = decide_adaptive_actions(
        completed_tools={PROFILE_DATASET, ANALYZE_MISSING},
        results=[heavy_missing],
        inspected_columns=set(),
        planned_tools={PROFILE_DATASET, ANALYZE_MISSING},
    )
    clean_actions = decide_adaptive_actions(
        completed_tools={PROFILE_DATASET, ANALYZE_MISSING},
        results=[clean_missing],
        inspected_columns=set(),
        planned_tools={PROFILE_DATASET, ANALYZE_MISSING},
    )
    assert any(action.tool_name == INSPECT_COLUMN for action in heavy_actions)
    assert not any(action.tool_name == INSPECT_COLUMN for action in clean_actions)


@pytest.mark.asyncio
async def test_loop_limit_stops_runaway_tool_calls() -> None:
    frame = parse_csv_bytes((FIXTURES_DIR / "survey_quality.csv").read_bytes())
    settings = Settings(
        planner_backend="local",
        agent_max_tool_calls=1,
        agent_max_iterations=20,
        agent_max_runtime_seconds=30,
    )
    loop = AgentLoop(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
        selected_tools=[PROFILE_DATASET, ANALYZE_MISSING, ANALYZE_DUPLICATES],
    )
    mission = Mission(goal="Analyze quality problems", dataset_id="ds")

    async def persist() -> None:
        return None

    await loop.run(mission, ToolContext("ds", "survey.csv", frame), persist)
    assert mission.agent_plan is not None
    assert mission.agent_plan.tool_call_count == 1
    assert mission.agent_plan.status == "LIMIT_REACHED"
    event_types = [event.type for event in mission.events]
    assert EventType.AGENT_LOOP_LIMIT_REACHED in event_types
    assert mission.investigation_report is not None
    assert any(task.status == StepStatus.SKIPPED for task in mission.agent_plan.tasks)


@pytest.mark.asyncio
async def test_tool_failure_is_recorded() -> None:
    frame = parse_csv_bytes((FIXTURES_DIR / "clean_numeric.csv").read_bytes())
    settings = Settings(planner_backend="local")
    loop = AgentLoop(
        reasoner=LocalFallbackReasoner(),
        settings=settings,
        plan_source=PlannerSource.LOCAL_FALLBACK,
    )
    mission = Mission(goal="fail tool", dataset_id="ds")
    task = AgentTask(
        task_id="task_bad",
        tool_name=INSPECT_COLUMN,
        objective="inspect missing",
        arguments={"column_name": "does_not_exist"},
    )
    mission.agent_plan = AgentPlan(
        objective="fail",
        source=PlannerSource.LOCAL_FALLBACK,
        selected_tools=[INSPECT_COLUMN],
        tasks=[task],
    )
    mission.execution_plan = mission.agent_plan.to_execution_plan()

    async def persist() -> None:
        return None

    with pytest.raises(Exception, match="not in the mission dataset"):
        await loop._execute_task(
            mission, ToolContext("ds", "clean.csv", frame), task, persist
        )
    assert task.status == StepStatus.FAILED
    assert mission.tool_invocations[-1].status == StepStatus.FAILED
    assert any(event.type == EventType.TOOL_FAILED for event in mission.events)
