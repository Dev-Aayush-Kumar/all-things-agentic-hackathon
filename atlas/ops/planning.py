"""Build and extend supervisor delegation plans from goals and evidence."""

from __future__ import annotations

from atlas.agent.policy import decide_adaptive_actions, select_tools, understand_goal
from atlas.agent.tools import INSPECT_COLUMN, PROFILE_DATASET
from atlas.domain.enums import PlannerSource, StepStatus
from atlas.domain.models import (
    AgentPlan,
    AgentTask,
    DelegationPlan,
    Mission,
    SpecialistFollowUp,
    SpecialistTask,
)
from atlas.investigation.missing import MATERIAL_MISSING_PERCENT
from atlas.ops.actions.policy import propose_action_follow_ups
from atlas.ops.registry import (
    CAPABILITY_INVESTIGATE,
    CAPABILITY_INVESTIGATE_COLUMN,
    CAPABILITY_SYNTHESIZE,
    AgentRegistry,
)
from atlas.ops.workspace import MissionWorkspace


def initial_analyst_tools(goal: str, selected_tools: list[str] | None) -> list[str]:
    tools = list(selected_tools or select_tools(goal))
    if PROFILE_DATASET not in tools:
        tools = [PROFILE_DATASET, *tools]
    return tools


def build_initial_delegation(
    mission: Mission,
    *,
    tools: list[str],
    source: PlannerSource,
    registry: AgentRegistry,
    max_attempts: int,
) -> DelegationPlan:
    """Create analyst work from the goal. Does not pre-schedule investigator/reporter."""
    objective = understand_goal(mission.goal)
    profile_task_id = "spec_profile"
    tasks: list[SpecialistTask] = [
        SpecialistTask(
            task_id=profile_task_id,
            mission_id=mission.mission_id,
            agent_id=registry.match(PROFILE_DATASET).id,
            objective="Profile the dataset before other measurements",
            capability=PROFILE_DATASET,
            depends_on=[],
            critical=True,
            max_attempts=max_attempts,
        )
    ]
    for tool in tools:
        if tool == PROFILE_DATASET:
            continue
        agent = registry.match(tool)
        tasks.append(
            SpecialistTask(
                task_id=f"spec_{tool}",
                mission_id=mission.mission_id,
                agent_id=agent.id,
                objective=f"Measure {tool}",
                capability=tool,
                depends_on=[profile_task_id],
                critical=False,
                max_attempts=max_attempts,
            )
        )
    mission.agent_plan = AgentPlan(
        objective=objective,
        source=source,
        selected_tools=list(tools),
        tasks=_tool_plan_tasks(tools),
        status="IN_PROGRESS",
        max_iterations=12,
    )
    mission.execution_plan = mission.agent_plan.to_execution_plan()
    mission.current_objective = objective
    return DelegationPlan(objective=objective, source=source, tasks=tasks)


def _tool_plan_tasks(tools: list[str]) -> list[AgentTask]:
    profile_id = "task_1"
    tasks = [
        AgentTask(
            task_id=profile_id,
            tool_name=PROFILE_DATASET,
            objective="Profile dataset shape, types, and numeric statistics",
            depends_on=[],
        )
    ]
    index = 2
    for tool in tools:
        if tool == PROFILE_DATASET:
            continue
        tasks.append(
            AgentTask(
                task_id=f"task_{index}",
                tool_name=tool,
                objective=f"Run capability {tool}",
                depends_on=[profile_id],
            )
        )
        index += 1
    return tasks


def ready_tasks(plan: DelegationPlan) -> list[SpecialistTask]:
    by_id = {task.task_id: task for task in plan.tasks}
    ready: list[SpecialistTask] = []
    for task in plan.tasks:
        if task.status != StepStatus.PENDING:
            continue
        if not all(
            dep in by_id and by_id[dep].status == StepStatus.COMPLETED
            for dep in task.depends_on
        ):
            continue
        ready.append(task)
    return ready


def has_open_work(plan: DelegationPlan) -> bool:
    return any(
        task.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
        for task in plan.tasks
    )


def task_exists(
    plan: DelegationPlan, capability: str, inputs: dict | None = None
) -> bool:
    inputs = inputs or {}
    wanted = {key: value for key, value in inputs.items() if key != "adaptive"}
    for task in plan.tasks:
        comparable = {
            key: value for key, value in task.inputs.items() if key != "adaptive"
        }
        if task.capability == capability and comparable == wanted:
            return True
    return False


def action_follow_ups(workspace: MissionWorkspace) -> list[SpecialistFollowUp]:
    """Supervisor-owned action proposals. Gemini never executes these."""
    return propose_action_follow_ups(workspace)


def observe_follow_ups(workspace: MissionWorkspace) -> list[SpecialistFollowUp]:
    """Evidence-driven work after a completed wave. Reporter is decided by the supervisor."""
    plan = workspace.mission.delegation_plan
    assert plan is not None
    follow_ups: list[SpecialistFollowUp] = []

    planned_tools = (
        set(workspace.mission.agent_plan.selected_tools)
        if workspace.mission.agent_plan
        else set()
    )
    completed_tools = {
        task.tool_name
        for task in (workspace.mission.agent_plan.tasks if workspace.mission.agent_plan else [])
        if task.status == StepStatus.COMPLETED
    }
    adaptive = decide_adaptive_actions(
        completed_tools=completed_tools,
        results=workspace.tool_results,
        inspected_columns=workspace.inspected_columns,
        planned_tools=planned_tools,
    )
    for action in adaptive:
        follow_ups.append(
            SpecialistFollowUp(
                capability=action.tool_name,
                objective=action.reason,
                arguments={**action.arguments, "adaptive": True},
                reason=action.reason,
            )
        )

    inspect_pending = any(
        item.capability == INSPECT_COLUMN for item in follow_ups
    ) or any(
        task.capability == INSPECT_COLUMN and task.status != StepStatus.COMPLETED
        for task in plan.tasks
    )
    if (
        workspace.mission.findings
        and not _investigator_already_scheduled(plan)
        and not inspect_pending
    ):
        column = _first_material_missing_column(workspace)
        if column:
            follow_ups.append(
                SpecialistFollowUp(
                    capability=CAPABILITY_INVESTIGATE_COLUMN,
                    objective=f"Investigate likely causes for issues in '{column}'",
                    arguments={"column_name": column},
                    reason=f"Evidence shows material problems in '{column}'",
                )
            )
        else:
            follow_ups.append(
                SpecialistFollowUp(
                    capability=CAPABILITY_INVESTIGATE,
                    objective="Examine measured findings for related causes",
                    arguments={},
                    reason="Findings exist and have not yet been investigated",
                )
            )

    for task in plan.tasks:
        if task.status == StepStatus.COMPLETED and task.result:
            follow_ups.extend(task.result.follow_ups)

    return follow_ups


def synthesis_follow_up() -> SpecialistFollowUp:
    return SpecialistFollowUp(
        capability=CAPABILITY_SYNTHESIZE,
        objective="Synthesize verified findings into the final report",
        arguments={},
        critical=True,
        reason="Analysis work is complete enough to produce a report",
    )


def _investigator_already_scheduled(plan: DelegationPlan) -> bool:
    return any(
        task.capability in {CAPABILITY_INVESTIGATE, CAPABILITY_INVESTIGATE_COLUMN}
        for task in plan.tasks
    )


def _first_material_missing_column(workspace: MissionWorkspace) -> str | None:
    for finding in workspace.mission.findings:
        percent = finding.evidence.get("missing_percent")
        material = finding.evidence.get("materially_incomplete") is True
        if material or (
            isinstance(percent, (int, float)) and percent >= MATERIAL_MISSING_PERCENT
        ):
            if finding.affected_columns:
                return finding.affected_columns[0]
    return None


def append_follow_up(
    mission: Mission,
    follow_up: SpecialistFollowUp,
    *,
    registry: AgentRegistry,
    max_attempts: int,
    depends_on: list[str],
) -> SpecialistTask | None:
    plan = mission.delegation_plan
    assert plan is not None
    if task_exists(plan, follow_up.capability, follow_up.arguments):
        return None
    descriptor = registry.match(follow_up.capability)
    task = SpecialistTask(
        task_id=f"spec_{follow_up.capability}_{len(plan.tasks) + 1}",
        mission_id=mission.mission_id,
        agent_id=descriptor.id,
        objective=follow_up.objective,
        capability=follow_up.capability,
        inputs=dict(follow_up.arguments),
        depends_on=depends_on,
        critical=follow_up.critical or follow_up.capability == CAPABILITY_SYNTHESIZE,
        max_attempts=max_attempts,
    )
    plan.tasks.append(task)
    return task
