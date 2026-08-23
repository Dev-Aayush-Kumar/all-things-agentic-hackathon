"""Local fallback planner unit tests."""

import pytest

from atlas.agent.local_planner import LocalFallbackPlanner
from atlas.domain.enums import PlannerSource, StepStatus


@pytest.mark.asyncio
async def test_local_planner_dataset_goal() -> None:
    planner = LocalFallbackPlanner()
    plan = await planner.create_plan(
        "Analyze the provided dataset and identify the major inconsistencies."
    )
    assert plan.planner_source == PlannerSource.LOCAL_FALLBACK
    assert len(plan.steps) >= 4
    assert plan.steps[0].status == StepStatus.PENDING
    assert any("dataset" in step.title.lower() for step in plan.steps)


@pytest.mark.asyncio
async def test_local_planner_generic_goal() -> None:
    planner = LocalFallbackPlanner()
    plan = await planner.create_plan("Deploy the updated configuration.")
    assert plan.planner_source == PlannerSource.LOCAL_FALLBACK
    assert len(plan.steps) >= 3
