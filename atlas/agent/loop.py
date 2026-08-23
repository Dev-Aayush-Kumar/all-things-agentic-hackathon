"""Agent orchestration loop.

GOAL → UNDERSTAND → PLAN → SELECT → USE TOOLS → OBSERVE → REASON → ADAPT → COMPLETE

Tools produce observed facts. The reasoner produces interpretation.
Loop limits are enforced. Unknown tools cannot be invoked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from atlas.agent.policy import (
    AdaptiveAction,
    decide_adaptive_actions,
    select_tools,
    tasks_from_tools,
    understand_goal,
)
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.agent.tools import (
    ANALYZE_CONSISTENCY,
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    ANALYZE_TYPE_FORMAT,
    INSPECT_COLUMN,
    PROFILE_DATASET,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSecurityError,
    invoke_tool,
)
from atlas.config.settings import Settings
from atlas.domain.enums import AgentPhase, EventType, PlannerSource, StepStatus
from atlas.domain.models import (
    AgentInterpretation,
    AgentPlan,
    AgentTask,
    DatasetProfile,
    EvidenceRecord,
    Finding,
    Mission,
    MissionEvent,
    ToolInvocation,
    utc_now,
)
from atlas.investigation.pipeline import InvestigationResult
from atlas.investigation.prioritize import prioritize_findings
from atlas.investigation.report import build_report

logger = logging.getLogger(__name__)

PersistFn = Callable[[], Awaitable[None]]

TOOL_COMPLETION_EVENTS = {
    PROFILE_DATASET: EventType.DATASET_PROFILE_COMPLETED,
    ANALYZE_MISSING: EventType.MISSING_DATA_ANALYSIS_COMPLETED,
    ANALYZE_DUPLICATES: EventType.DUPLICATE_ANALYSIS_COMPLETED,
    ANALYZE_TYPE_FORMAT: EventType.TYPE_FORMAT_ANALYSIS_COMPLETED,
    ANALYZE_OUTLIERS: EventType.OUTLIER_ANALYSIS_COMPLETED,
    ANALYZE_CONSISTENCY: EventType.CONSISTENCY_ANALYSIS_COMPLETED,
}


class AgentLoop:
    """Runs the constrained investigation agent loop for a dataset mission."""

    def __init__(
        self,
        *,
        reasoner: InvestigationReasoner,
        settings: Settings,
        plan_source: PlannerSource,
        selected_tools: list[str] | None = None,
        step_delay_seconds: float = 0.0,
    ) -> None:
        self._reasoner = reasoner
        self._settings = settings
        self._plan_source = plan_source
        self._selected_tools = selected_tools
        self._step_delay_seconds = step_delay_seconds

    async def run(
        self,
        mission: Mission,
        context: ToolContext,
        persist: PersistFn,
    ) -> None:
        started = time.monotonic()
        mission.current_phase = AgentPhase.UNDERSTANDING
        understanding = understand_goal(mission.goal)
        _add_event(
            mission,
            EventType.MISSION_UNDERSTOOD,
            "Mission understood",
            {"objective": understanding, "source": self._plan_source.value},
        )
        await persist()

        mission.current_phase = AgentPhase.PLANNING
        tools = self._selected_tools or select_tools(mission.goal)
        if PROFILE_DATASET not in tools:
            tools = [PROFILE_DATASET, *tools]
        tasks = tasks_from_tools(tools)
        mission.agent_plan = AgentPlan(
            objective=understanding,
            source=self._plan_source,
            selected_tools=list(tools),
            tasks=tasks,
            status="IN_PROGRESS",
            max_iterations=self._settings.agent_max_iterations,
        )
        mission.execution_plan = mission.agent_plan.to_execution_plan()
        _add_event(
            mission,
            EventType.AGENT_PLAN_CREATED,
            "Agent plan created",
            {
                "selected_tools": tools,
                "task_count": len(tasks),
                "source": self._plan_source.value,
            },
        )
        _add_event(
            mission,
            EventType.AGENT_DECISION,
            "Initial capabilities selected from the mission goal",
            {"selected_tools": tools},
        )
        await persist()

        results: list[ToolResult] = []
        collected_findings: list[Finding] = []
        profile: DatasetProfile | None = None
        inspected_columns: set[str] = set()
        planned_tools = set(tools)
        completed_tools: set[str] = set()
        hit_limit = False

        while True:
            plan = mission.agent_plan
            assert plan is not None
            plan.iteration += 1
            if plan.iteration > self._settings.agent_max_iterations:
                hit_limit = True
                _add_event(
                    mission,
                    EventType.AGENT_LOOP_LIMIT_REACHED,
                    "Agent iteration limit reached",
                    {"max_iterations": self._settings.agent_max_iterations},
                )
                break
            if time.monotonic() - started > self._settings.agent_max_runtime_seconds:
                hit_limit = True
                _add_event(
                    mission,
                    EventType.AGENT_LOOP_LIMIT_REACHED,
                    "Agent runtime limit reached",
                    {"max_runtime_seconds": self._settings.agent_max_runtime_seconds},
                )
                break

            pending = [task for task in plan.tasks if task.status == StepStatus.PENDING]
            if not pending:
                break
            if plan.tool_call_count >= self._settings.agent_max_tool_calls:
                hit_limit = True
                _add_event(
                    mission,
                    EventType.AGENT_LOOP_LIMIT_REACHED,
                    "Agent tool-call limit reached",
                    {"max_tool_calls": self._settings.agent_max_tool_calls},
                )
                break

            task = pending[0]
            mission.current_phase = AgentPhase.TOOL_EXECUTION
            mission.current_task = task.tool_name
            plan.current_task_id = task.task_id
            result = await self._execute_task(mission, context, task, persist)
            plan.tool_call_count += 1
            results.append(result)
            collected_findings.extend(result.findings)
            if result.profile is not None:
                profile = result.profile
            completed_tools.add(task.tool_name)
            if task.tool_name == INSPECT_COLUMN:
                column = task.arguments.get("column_name")
                if isinstance(column, str):
                    inspected_columns.add(column)

            mission.current_phase = AgentPhase.OBSERVING
            adaptive = decide_adaptive_actions(
                completed_tools=completed_tools,
                results=results,
                inspected_columns=inspected_columns,
                planned_tools=planned_tools,
            )
            new_tasks = self._enqueue_adaptive(mission, adaptive)
            if new_tasks:
                planned_tools.update(item.tool_name for item in new_tasks)
                mission.current_phase = AgentPhase.ADAPTING
            await persist()
            await self._pause()

        if hit_limit and mission.agent_plan is not None:
            for task in mission.agent_plan.tasks:
                if task.status == StepStatus.PENDING:
                    task.status = StepStatus.SKIPPED
                    task.result_summary = "Skipped because an agent loop limit was reached"
            mission.agent_plan.status = "LIMIT_REACHED"
            mission.execution_plan = mission.agent_plan.to_execution_plan()

        if hit_limit and profile is None:
            raise RuntimeError("Agent stopped at loop limit before producing a dataset profile")

        await self._finalize(mission, context, collected_findings, profile, persist)

    async def _execute_task(
        self,
        mission: Mission,
        context: ToolContext,
        task: AgentTask,
        persist: PersistFn,
    ) -> ToolResult:
        task.status = StepStatus.IN_PROGRESS
        _sync_execution_step(mission, task)
        _add_event(
            mission,
            EventType.TOOL_SELECTED,
            f"Tool selected: {task.tool_name}",
            {
                "tool_name": task.tool_name,
                "task_id": task.task_id,
                "adaptive": task.adaptive,
            },
        )
        _add_event(
            mission,
            EventType.TOOL_STARTED,
            f"Tool started: {task.tool_name}",
            {"tool_name": task.tool_name, "arguments": task.arguments},
        )
        invocation = ToolInvocation(
            tool_name=task.tool_name,
            arguments=task.arguments,
            status=StepStatus.IN_PROGRESS,
            adaptive=task.adaptive,
        )
        mission.tool_invocations.append(invocation)
        await persist()

        try:
            result = await asyncio.to_thread(
                invoke_tool, context, task.tool_name, **task.arguments
            )
        except (ToolSecurityError, ToolExecutionError) as exc:
            task.status = StepStatus.FAILED
            task.result_summary = str(exc)
            invocation.status = StepStatus.FAILED
            invocation.error = str(exc)
            invocation.completed_at = utc_now()
            _sync_execution_step(mission, task)
            _add_event(
                mission,
                EventType.TOOL_FAILED,
                f"Tool failed: {task.tool_name}",
                {"tool_name": task.tool_name, "error": str(exc)},
            )
            await persist()
            raise

        evidence = EvidenceRecord(
            tool_name=task.tool_name,
            task_id=task.task_id,
            observed_facts=result.observed_facts,
            finding_ids=[finding.finding_id for finding in result.findings],
        )
        mission.evidence_records.append(evidence)
        task.status = StepStatus.COMPLETED
        task.evidence_id = evidence.evidence_id
        task.result_summary = result.summary
        invocation.status = StepStatus.COMPLETED
        invocation.evidence_id = evidence.evidence_id
        invocation.completed_at = utc_now()
        _sync_execution_step(mission, task)

        _add_event(
            mission,
            EventType.TOOL_COMPLETED,
            f"Tool completed: {task.tool_name}",
            {"tool_name": task.tool_name, "summary": result.summary},
        )
        _add_event(
            mission,
            EventType.EVIDENCE_RECEIVED,
            f"Evidence received from {task.tool_name}",
            {
                "evidence_id": evidence.evidence_id,
                "tool_name": task.tool_name,
                "finding_count": len(result.findings),
            },
        )
        stage_event = TOOL_COMPLETION_EVENTS.get(task.tool_name)
        if stage_event is not None:
            _add_event(
                mission,
                stage_event,
                result.summary,
                {"tool_name": task.tool_name, "finding_count": len(result.findings)},
            )
        await persist()
        return result

    def _enqueue_adaptive(
        self,
        mission: Mission,
        actions: list[AdaptiveAction],
    ) -> list[AgentTask]:
        assert mission.agent_plan is not None
        created: list[AgentTask] = []
        next_index = len(mission.agent_plan.tasks) + 1
        for action in actions:
            already = any(
                task.tool_name == action.tool_name
                and task.arguments == action.arguments
                for task in mission.agent_plan.tasks
            )
            if already:
                continue
            new_tasks = tasks_from_tools(
                [action.tool_name],
                adaptive=True,
                arguments_by_tool={action.tool_name: action.arguments},
                reason=action.reason,
                start_index=next_index,
            )
            next_index += len(new_tasks)
            mission.agent_plan.tasks.extend(new_tasks)
            mission.agent_plan.selected_tools = list(
                dict.fromkeys([*mission.agent_plan.selected_tools, action.tool_name])
            )
            created.extend(new_tasks)
            _add_event(
                mission,
                EventType.ADAPTIVE_INVESTIGATION_TRIGGERED,
                "Adaptive investigation triggered",
                {
                    "tool_name": action.tool_name,
                    "arguments": action.arguments,
                    "reason": action.reason,
                },
            )
            _add_event(
                mission,
                EventType.AGENT_DECISION,
                action.reason,
                {"tool_name": action.tool_name, "adaptive": True},
            )
        if created:
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        return created

    async def _finalize(
        self,
        mission: Mission,
        context: ToolContext,
        findings: list[Finding],
        profile: DatasetProfile | None,
        persist: PersistFn,
    ) -> None:
        if profile is None:
            raise RuntimeError("Agent completed without a dataset profile")

        ranked = prioritize_findings(findings)
        _add_event(
            mission,
            EventType.FINDINGS_PRIORITIZED,
            "Findings prioritized",
            {"finding_count": len(ranked)},
        )
        await persist()

        mission.current_phase = AgentPhase.REASONING
        reasoning = await self._reasoner.interpret(mission.goal, profile, ranked)
        evidence_ids = [record.evidence_id for record in mission.evidence_records]
        finding_ids = [finding.finding_id for finding in ranked]
        interpretations = [
            AgentInterpretation(
                kind="mission_summary",
                text=reasoning.mission_summary,
                related_evidence_ids=evidence_ids,
                related_finding_ids=finding_ids,
            ),
            AgentInterpretation(
                kind="investigation_summary",
                text=reasoning.investigation_summary,
                related_evidence_ids=evidence_ids,
                related_finding_ids=finding_ids,
            ),
            AgentInterpretation(
                kind="overall_assessment",
                text=reasoning.overall_assessment,
                related_evidence_ids=evidence_ids,
                related_finding_ids=finding_ids,
            ),
        ]
        mission.interpretations.extend(interpretations)
        _add_event(
            mission,
            EventType.FINAL_REASONING_COMPLETED,
            "Final reasoning completed",
            {"reasoning_source": reasoning.source.value},
        )

        result = InvestigationResult(profile=profile, findings=ranked, frame=context.frame)
        mission.investigation_report = build_report(
            dataset_id=context.dataset_id,
            original_filename=context.original_filename,
            result=result,
            mission_summary=reasoning.mission_summary,
            investigation_summary=reasoning.investigation_summary,
            overall_assessment=reasoning.overall_assessment,
            recommended_actions=reasoning.recommended_actions,
            reasoning_source=reasoning.source,
            evidence_records=list(mission.evidence_records),
        )
        mission.investigation_report.interpretations = list(mission.interpretations)
        mission.investigation_report.evidence_records = list(mission.evidence_records)

        if mission.agent_plan is not None:
            if mission.agent_plan.status != "LIMIT_REACHED":
                mission.agent_plan.status = "COMPLETED"
            mission.agent_plan.current_task_id = None
            mission.execution_plan = mission.agent_plan.to_execution_plan()

        mission.current_phase = AgentPhase.COMPLETING
        mission.current_task = None
        _add_event(
            mission,
            EventType.FINAL_REPORT_GENERATED,
            "Final report generated",
            {
                "finding_count": len(ranked),
                "reasoning_source": reasoning.source.value,
                "interpretation_count": len(interpretations),
            },
        )
        await persist()

    async def _pause(self) -> None:
        if self._step_delay_seconds > 0:
            await asyncio.sleep(self._step_delay_seconds)


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
