"""Mission supervisor / orchestrator.

Owns the mission-level decision loop: understand, delegate, observe, replan, synthesize.
"""

from __future__ import annotations

import asyncio
import logging
import time

from atlas.agent.policy import understand_goal
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.agent.tools import INVESTIGATION_TOOLS, ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import AgentPhase, EventType, PlannerSource, StepStatus
from atlas.domain.models import Mission, MissionEvent
from atlas.ops.delegation import LocalDelegationManager
from atlas.ops.planning import (
    append_follow_up,
    build_initial_delegation,
    has_open_work,
    initial_analyst_tools,
    observe_follow_ups,
    ready_tasks,
    synthesis_follow_up,
    task_exists,
)
from atlas.ops.registry import CAPABILITY_SYNTHESIZE, AgentRegistry, default_registry
from atlas.ops.specialists import build_specialists
from atlas.ops.workspace import MissionWorkspace, PersistFn

logger = logging.getLogger(__name__)


class CriticalTaskFailedError(RuntimeError):
    """Raised when a critical specialist cannot complete its objective."""


class Supervisor:
    """Delegates work to specialists and replans from observed evidence."""

    def __init__(
        self,
        *,
        reasoner: InvestigationReasoner,
        settings: Settings,
        plan_source: PlannerSource,
        selected_tools: list[str] | None = None,
        registry: AgentRegistry | None = None,
        step_delay_seconds: float = 0.0,
        specialists: dict | None = None,
    ) -> None:
        self._reasoner = reasoner
        self._settings = settings
        self._plan_source = plan_source
        self._selected_tools = selected_tools
        self._registry = registry or default_registry()
        self._step_delay_seconds = step_delay_seconds
        self._specialists = specialists or build_specialists(self._registry)
        self._delegation = LocalDelegationManager(self._specialists)

    async def run(
        self,
        mission: Mission,
        context: ToolContext,
        persist: PersistFn,
    ) -> None:
        started = time.monotonic()
        workspace = MissionWorkspace(
            mission=mission,
            tool_context=context,
            persist=persist,
            lock=asyncio.Lock(),
            settings=self._settings,
            reasoner=self._reasoner,
            registry=self._registry,
            plan_source=self._plan_source,
            step_delay_seconds=self._step_delay_seconds,
        )
        self._restore_inspected(workspace)

        mission.current_phase = AgentPhase.UNDERSTANDING
        understanding = understand_goal(mission.goal)
        mission.current_objective = understanding
        if not any(event.type == EventType.MISSION_UNDERSTOOD for event in mission.events):
            _add_event(
                mission,
                EventType.MISSION_UNDERSTOOD,
                "Mission understood",
                {"objective": understanding, "source": self._plan_source.value},
            )
            await persist()

        resuming = self._prepare_plan(mission)
        if not resuming:
            mission.current_phase = AgentPhase.PLANNING
            tools = initial_analyst_tools(mission.goal, self._selected_tools)
            mission.delegation_plan = build_initial_delegation(
                mission,
                tools=tools,
                source=self._plan_source,
                registry=self._registry,
                max_attempts=self._settings.specialist_task_max_attempts,
            )
            if mission.agent_plan is not None:
                mission.agent_plan.max_iterations = self._settings.agent_max_iterations
            _add_event(
                mission,
                EventType.DELEGATION_PLAN_CREATED,
                "Delegation plan created",
                {
                    "task_count": len(mission.delegation_plan.tasks),
                    "source": self._plan_source.value,
                    "agent_ids": sorted(
                        {task.agent_id for task in mission.delegation_plan.tasks}
                    ),
                },
            )
            _add_event(
                mission,
                EventType.AGENT_PLAN_CREATED,
                "Agent plan created",
                {
                    "selected_tools": tools,
                    "task_count": len(tools),
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
        else:
            logger.info("Resuming mission %s from persisted specialist tasks", mission.mission_id)

        while True:
            plan = mission.delegation_plan
            assert plan is not None
            plan.wave += 1
            if plan.wave > self._settings.agent_max_iterations:
                self._hit_limit(mission, "Supervisor iteration limit reached")
                break
            if time.monotonic() - started > self._settings.agent_max_runtime_seconds:
                self._hit_limit(mission, "Supervisor runtime limit reached")
                break
            if (
                mission.agent_plan is not None
                and mission.agent_plan.tool_call_count >= self._settings.agent_max_tool_calls
                and ready_tasks(plan)
            ):
                self._hit_limit(mission, "Agent tool-call limit reached")
                break

            ready = ready_tasks(plan)
            if ready:
                mission.current_phase = AgentPhase.DELEGATING
                plan.current_task_ids = [task.task_id for task in ready]
                await persist()
                await self._delegation.execute_ready(ready, workspace)
                self._raise_if_critical_exhausted(plan)
                mission.current_phase = AgentPhase.OBSERVING
                _add_event(
                    mission,
                    EventType.SUPERVISOR_OBSERVED,
                    "Supervisor observed specialist results",
                    {
                        "wave": plan.wave,
                        "completed": [
                            task.task_id
                            for task in plan.tasks
                            if task.status == StepStatus.COMPLETED
                        ],
                    },
                )
                await persist()
                continue

            follow_ups = observe_follow_ups(workspace)
            added = self._apply_follow_ups(mission, follow_ups, adaptive=True)
            if added:
                mission.current_phase = AgentPhase.ADAPTING
                plan.replan_count += 1
                _add_event(
                    mission,
                    EventType.REPLAN_TRIGGERED,
                    "Supervisor replanned after observing evidence",
                    {
                        "added_task_ids": [task.task_id for task in added],
                        "replan_count": plan.replan_count,
                    },
                )
                _add_event(
                    mission,
                    EventType.AGENT_DECISION,
                    "Additional specialist work is justified by observed evidence",
                    {
                        "capabilities": [task.capability for task in added],
                    },
                )
                await persist()
                continue

            if (
                mission.dataset_profile is not None
                and not has_open_work(plan)
                and not task_exists(plan, CAPABILITY_SYNTHESIZE)
            ):
                reporter = append_follow_up(
                    mission,
                    synthesis_follow_up(),
                    registry=self._registry,
                    max_attempts=self._settings.specialist_task_max_attempts,
                    depends_on=_completed_ids(plan),
                )
                if reporter is not None:
                    mission.current_phase = AgentPhase.SYNTHESIZING
                    _add_event(
                        mission,
                        EventType.SYNTHESIS_STARTED,
                        "Synthesis started",
                        {"task_id": reporter.task_id},
                    )
                    await persist()
                    continue

            break

        await self._ensure_report(workspace)

        if mission.dataset_profile is None:
            raise RuntimeError("Supervisor finished without a dataset profile")

        plan = mission.delegation_plan
        assert plan is not None
        if any(
            task.capability == CAPABILITY_SYNTHESIZE and task.status == StepStatus.COMPLETED
            for task in plan.tasks
        ):
            _add_event(
                mission,
                EventType.SYNTHESIS_COMPLETED,
                "Synthesis completed",
                {"reasoning_source": (
                    mission.investigation_report.reasoning_source.value
                    if mission.investigation_report
                    else self._plan_source.value
                )},
            )
            _add_event(
                mission,
                EventType.FINAL_REASONING_COMPLETED,
                "Final reasoning completed",
                {
                    "reasoning_source": (
                        mission.investigation_report.reasoning_source.value
                        if mission.investigation_report
                        else self._plan_source.value
                    )
                },
            )
            _add_event(
                mission,
                EventType.FINAL_REPORT_GENERATED,
                "Final report generated",
                {
                    "finding_count": len(mission.findings),
                    "reasoning_source": (
                        mission.investigation_report.reasoning_source.value
                        if mission.investigation_report
                        else self._plan_source.value
                    ),
                    "interpretation_count": len(mission.interpretations),
                },
            )
            _add_event(
                mission,
                EventType.FINDINGS_PRIORITIZED,
                "Findings prioritized",
                {"finding_count": len(mission.findings)},
            )

        if mission.agent_plan is not None:
            if mission.agent_plan.status != "LIMIT_REACHED":
                mission.agent_plan.status = "COMPLETED"
            mission.agent_plan.current_task_id = None
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        plan.status = "COMPLETED"
        plan.current_task_ids = []
        mission.current_phase = AgentPhase.COMPLETING
        mission.current_task = None
        await persist()

    def _prepare_plan(self, mission: Mission) -> bool:
        plan = mission.delegation_plan
        if plan is None or not plan.tasks:
            return False
        for task in plan.tasks:
            if task.status == StepStatus.IN_PROGRESS:
                task.status = StepStatus.PENDING
                task.started_at = None
                task.error = "Interrupted; will retry from persisted state"
        if mission.agent_plan is not None:
            for task in mission.agent_plan.tasks:
                if task.status == StepStatus.IN_PROGRESS:
                    task.status = StepStatus.PENDING
        return any(task.status == StepStatus.COMPLETED for task in plan.tasks)

    def _apply_follow_ups(self, mission: Mission, follow_ups, *, adaptive: bool) -> list:
        plan = mission.delegation_plan
        assert plan is not None
        added = []
        completed_ids = _completed_ids(plan)
        for follow_up in follow_ups:
            task = append_follow_up(
                mission,
                follow_up,
                registry=self._registry,
                max_attempts=self._settings.specialist_task_max_attempts,
                depends_on=completed_ids,
            )
            if task is None:
                continue
            added.append(task)
            if follow_up.capability in INVESTIGATION_TOOLS:
                _add_event(
                    mission,
                    EventType.ADAPTIVE_INVESTIGATION_TRIGGERED,
                    "Adaptive investigation triggered",
                    {
                        "tool_name": follow_up.capability,
                        "arguments": {
                            key: value
                            for key, value in follow_up.arguments.items()
                            if key != "adaptive"
                        },
                        "reason": follow_up.reason,
                        "agent_id": task.agent_id,
                    },
                )
        return added

    def _raise_if_critical_exhausted(self, plan) -> None:
        for task in plan.tasks:
            if (
                task.critical
                and task.status == StepStatus.FAILED
                and task.attempt_count >= task.max_attempts
            ):
                raise CriticalTaskFailedError(
                    task.error or f"Critical task '{task.capability}' failed"
                )

    def _hit_limit(self, mission: Mission, message: str) -> None:
        if mission.agent_plan is not None:
            for task in mission.agent_plan.tasks:
                if task.status == StepStatus.PENDING:
                    task.status = StepStatus.SKIPPED
                    task.result_summary = "Skipped because an agent loop limit was reached"
            mission.agent_plan.status = "LIMIT_REACHED"
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        if mission.delegation_plan is not None:
            for task in mission.delegation_plan.tasks:
                if task.status == StepStatus.PENDING:
                    task.status = StepStatus.SKIPPED
                    task.error = message
            mission.delegation_plan.status = "LIMIT_REACHED"
        _add_event(
            mission,
            EventType.AGENT_LOOP_LIMIT_REACHED,
            message,
            {
                "max_iterations": self._settings.agent_max_iterations,
                "max_tool_calls": self._settings.agent_max_tool_calls,
            },
        )

    async def _ensure_report(self, workspace: MissionWorkspace) -> None:
        mission = workspace.mission
        if mission.investigation_report is not None or mission.dataset_profile is None:
            return
        plan = mission.delegation_plan
        if plan is None:
            return
        if task_exists(plan, CAPABILITY_SYNTHESIZE):
            synth = next(
                task
                for task in plan.tasks
                if task.capability == CAPABILITY_SYNTHESIZE
            )
            if synth.status == StepStatus.COMPLETED:
                return
            if synth.status in {StepStatus.PENDING, StepStatus.SKIPPED, StepStatus.FAILED}:
                if synth.status == StepStatus.FAILED:
                    return
                synth.status = StepStatus.PENDING
                await self._delegation.execute_ready([synth], workspace)
                return
        reporter = append_follow_up(
            mission,
            synthesis_follow_up(),
            registry=self._registry,
            max_attempts=self._settings.specialist_task_max_attempts,
            depends_on=[],
        )
        if reporter is not None:
            _add_event(
                mission,
                EventType.SYNTHESIS_STARTED,
                "Synthesis started",
                {"task_id": reporter.task_id},
            )
            await self._delegation.execute_ready([reporter], workspace)

    @staticmethod
    def _restore_inspected(workspace: MissionWorkspace) -> None:
        for record in workspace.mission.evidence_records:
            if record.tool_name != "inspect_column":
                continue
            column = record.observed_facts.get("column_name")
            if isinstance(column, str):
                workspace.inspected_columns.add(column)


def _completed_ids(plan) -> list[str]:
    return [task.task_id for task in plan.tasks if task.status == StepStatus.COMPLETED]


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
