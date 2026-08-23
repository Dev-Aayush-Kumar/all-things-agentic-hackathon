"""Allowlisted tool execution used by specialists.

Records evidence, tool invocations, and agent_plan tool tasks on the mission.
"""

from __future__ import annotations

import asyncio
import logging

from atlas.agent.policy import _tool_objective
from atlas.agent.tools import (
    ANALYZE_CONSISTENCY,
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    ANALYZE_TYPE_FORMAT,
    INSPECT_COLUMN,
    PROFILE_DATASET,
    ToolExecutionError,
    ToolResult,
    ToolSecurityError,
    invoke_tool,
)
from atlas.domain.enums import EventType, FindingCategory, StepStatus
from atlas.domain.models import (
    AgentTask,
    EvidenceRecord,
    Mission,
    MissionEvent,
    ToolInvocation,
    utc_now,
)
from atlas.ops.workspace import MissionWorkspace

logger = logging.getLogger(__name__)

TOOL_COMPLETION_EVENTS = {
    PROFILE_DATASET: EventType.DATASET_PROFILE_COMPLETED,
    ANALYZE_MISSING: EventType.MISSING_DATA_ANALYSIS_COMPLETED,
    ANALYZE_DUPLICATES: EventType.DUPLICATE_ANALYSIS_COMPLETED,
    ANALYZE_TYPE_FORMAT: EventType.TYPE_FORMAT_ANALYSIS_COMPLETED,
    ANALYZE_OUTLIERS: EventType.OUTLIER_ANALYSIS_COMPLETED,
    ANALYZE_CONSISTENCY: EventType.CONSISTENCY_ANALYSIS_COMPLETED,
}

_TOOL_FINDING_CATEGORY = {
    ANALYZE_MISSING: FindingCategory.MISSING_DATA,
    ANALYZE_DUPLICATES: FindingCategory.DUPLICATE_ROWS,
    ANALYZE_TYPE_FORMAT: FindingCategory.TYPE_FORMAT_ANOMALY,
    ANALYZE_OUTLIERS: FindingCategory.NUMERIC_OUTLIER,
    ANALYZE_CONSISTENCY: FindingCategory.CONSISTENCY_VIOLATION,
}


async def run_authorized_tool(
    workspace: MissionWorkspace,
    *,
    agent_id: str,
    tool_name: str,
    arguments: dict | None = None,
    adaptive: bool = False,
    specialist_task_id: str | None = None,
    reobserve: bool = False,
) -> ToolResult:
    """Execute a tool only if the specialist's descriptor allows it."""
    workspace.registry.authorize_tool(agent_id, tool_name)
    arguments = arguments or {}
    reobserve = reobserve or bool(arguments.get("reobserve"))
    invoke_arguments = {
        key: value
        for key, value in arguments.items()
        if key not in {"adaptive", "reobserve", "working_version"}
    }
    mission = workspace.mission

    async with workspace.lock:
        plan_task = _ensure_plan_task(
            mission, tool_name, arguments, adaptive=adaptive, reason=None
        )
        plan_task.status = StepStatus.IN_PROGRESS
        mission.current_phase = mission.current_phase
        mission.current_task = tool_name
        if mission.agent_plan is not None:
            mission.agent_plan.current_task_id = plan_task.task_id
            mission.agent_plan.tool_call_count += 1
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        _add_event(
            mission,
            EventType.TOOL_SELECTED,
            f"Tool selected: {tool_name}",
            {
                "tool_name": tool_name,
                "task_id": plan_task.task_id,
                "agent_id": agent_id,
                "adaptive": adaptive,
                "specialist_task_id": specialist_task_id,
            },
        )
        _add_event(
            mission,
            EventType.TOOL_STARTED,
            f"Tool started: {tool_name}",
            {"tool_name": tool_name, "arguments": arguments, "agent_id": agent_id},
        )
        invocation = ToolInvocation(
            tool_name=tool_name,
            arguments=arguments,
            status=StepStatus.IN_PROGRESS,
            adaptive=adaptive,
        )
        mission.tool_invocations.append(invocation)
        await workspace.persist()

    try:
        result = await asyncio.to_thread(
            invoke_tool, workspace.tool_context, tool_name, **invoke_arguments
        )
    except (ToolSecurityError, ToolExecutionError, PermissionError) as exc:
        async with workspace.lock:
            plan_task.status = StepStatus.FAILED
            plan_task.result_summary = str(exc)
            invocation.status = StepStatus.FAILED
            invocation.error = str(exc)
            invocation.completed_at = utc_now()
            _sync_execution_step(mission, plan_task)
            _add_event(
                mission,
                EventType.TOOL_FAILED,
                f"Tool failed: {tool_name}",
                {"tool_name": tool_name, "error": str(exc), "agent_id": agent_id},
            )
            await workspace.persist()
        raise

    async with workspace.lock:
        evidence = EvidenceRecord(
            tool_name=tool_name,
            task_id=plan_task.task_id,
            observed_facts=result.observed_facts,
            finding_ids=[finding.finding_id for finding in result.findings],
        )
        mission.evidence_records.append(evidence)
        if reobserve:
            category = _TOOL_FINDING_CATEGORY.get(tool_name)
            if category is not None:
                mission.findings = [
                    finding for finding in mission.findings if finding.category != category
                ]
        if result.findings:
            existing = {item.finding_id for item in mission.findings}
            mission.findings.extend(
                finding for finding in result.findings if finding.finding_id not in existing
            )
        if result.profile is not None:
            mission.dataset_profile = result.profile
        if tool_name == INSPECT_COLUMN:
            column = arguments.get("column_name")
            if isinstance(column, str):
                workspace.inspected_columns.add(column)
        plan_task.status = StepStatus.COMPLETED
        plan_task.evidence_id = evidence.evidence_id
        plan_task.result_summary = result.summary
        invocation.status = StepStatus.COMPLETED
        invocation.evidence_id = evidence.evidence_id
        invocation.completed_at = utc_now()
        workspace.tool_results.append(result)
        _sync_execution_step(mission, plan_task)
        _add_event(
            mission,
            EventType.TOOL_COMPLETED,
            f"Tool completed: {tool_name}",
            {"tool_name": tool_name, "summary": result.summary, "agent_id": agent_id},
        )
        _add_event(
            mission,
            EventType.EVIDENCE_RECEIVED,
            f"Evidence received from {tool_name}",
            {
                "evidence_id": evidence.evidence_id,
                "tool_name": tool_name,
                "finding_count": len(result.findings),
                "agent_id": agent_id,
            },
        )
        stage_event = TOOL_COMPLETION_EVENTS.get(tool_name)
        if stage_event is not None:
            _add_event(
                mission,
                stage_event,
                result.summary,
                {"tool_name": tool_name, "finding_count": len(result.findings)},
            )
        if mission.agent_plan is not None:
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        await workspace.persist()

    if workspace.step_delay_seconds > 0:
        await asyncio.sleep(workspace.step_delay_seconds)
    return result


def tool_already_completed(mission: Mission, tool_name: str, arguments: dict | None = None) -> bool:
    arguments = arguments or {}
    if mission.agent_plan is None:
        return False
    for task in mission.agent_plan.tasks:
        if (
            task.tool_name == tool_name
            and task.arguments == arguments
            and task.status == StepStatus.COMPLETED
        ):
            return True
    return False


def _ensure_plan_task(
    mission: Mission,
    tool_name: str,
    arguments: dict,
    *,
    adaptive: bool,
    reason: str | None,
) -> AgentTask:
    if mission.agent_plan is None:
        raise RuntimeError("Cannot run a tool before the agent plan exists")
    for task in mission.agent_plan.tasks:
        if task.tool_name == tool_name and task.arguments == arguments:
            return task
    task = AgentTask(
        task_id=f"task_{len(mission.agent_plan.tasks) + 1}",
        tool_name=tool_name,
        objective=_tool_objective(tool_name, arguments),
        adaptive=adaptive,
        arguments=arguments,
        decision_reason=reason,
    )
    mission.agent_plan.tasks.append(task)
    mission.agent_plan.selected_tools = list(
        dict.fromkeys([*mission.agent_plan.selected_tools, tool_name])
    )
    mission.execution_plan = mission.agent_plan.to_execution_plan()
    return task


def _add_event(
    mission: Mission,
    event_type: EventType,
    message: str,
    metadata: dict | None = None,
) -> None:
    mission.events.append(
        MissionEvent(type=event_type, message=message, metadata=metadata or {})
    )
    mission.touch()


def _sync_execution_step(mission: Mission, task: AgentTask) -> None:
    if mission.execution_plan is None:
        return
    for step in mission.execution_plan.steps:
        if step.id == task.task_id:
            step.status = task.status
            step.result = task.result_summary
            return
