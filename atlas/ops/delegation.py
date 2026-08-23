"""Local in-process delegation of specialist tasks.

The interface is the extension point for future Pub/Sub / Cloud Run specialists.
This round executes specialists in the mission worker process.
"""

from __future__ import annotations

import asyncio
import logging

from atlas.domain.enums import EventType, StepStatus
from atlas.domain.models import MissionEvent, SpecialistTask, utc_now
from atlas.ops.specialists import SpecialistAgent
from atlas.ops.workspace import MissionWorkspace

logger = logging.getLogger(__name__)


class LocalDelegationManager:
    """Runs ready specialist tasks, concurrently when they are independent."""

    def __init__(self, specialists: dict[str, SpecialistAgent]) -> None:
        self._specialists = specialists

    async def execute_ready(
        self,
        tasks: list[SpecialistTask],
        workspace: MissionWorkspace,
    ) -> list[SpecialistTask]:
        if not tasks:
            return []
        if len(tasks) == 1:
            await self._run_one(tasks[0], workspace)
            return tasks
        await asyncio.gather(*(self._run_one(task, workspace) for task in tasks))
        return tasks

    async def _run_one(self, task: SpecialistTask, workspace: MissionWorkspace) -> None:
        specialist = self._specialists.get(task.agent_id)
        if specialist is None:
            await self._fail(task, workspace, f"Unknown specialist '{task.agent_id}'")
            return

        async with workspace.lock:
            task.status = StepStatus.IN_PROGRESS
            task.started_at = utc_now()
            task.attempt_count += 1
            workspace.mission.current_phase = workspace.mission.current_phase
            workspace.mission.current_task = task.capability
            workspace.mission.current_objective = task.objective
            if workspace.mission.delegation_plan is not None:
                workspace.mission.delegation_plan.current_task_ids = [task.task_id]
            _event(
                workspace,
                EventType.TASK_DELEGATED,
                f"Task delegated to {task.agent_id}",
                {
                    "task_id": task.task_id,
                    "agent_id": task.agent_id,
                    "capability": task.capability,
                    "attempt": task.attempt_count,
                },
            )
            _event(
                workspace,
                EventType.AGENT_STARTED,
                f"Agent started: {task.agent_id}",
                {
                    "task_id": task.task_id,
                    "agent_id": task.agent_id,
                    "capability": task.capability,
                },
            )
            logger.info(
                "Delegating mission=%s task=%s agent=%s capability=%s attempt=%s",
                task.mission_id,
                task.task_id,
                task.agent_id,
                task.capability,
                task.attempt_count,
            )
            await workspace.persist()

        try:
            result = await specialist.execute(task, workspace)
        except Exception as exc:
            logger.exception(
                "Specialist failed mission=%s task=%s agent=%s",
                task.mission_id,
                task.task_id,
                task.agent_id,
            )
            await self._fail(task, workspace, str(exc))
            return

        async with workspace.lock:
            task.status = StepStatus.COMPLETED
            task.completed_at = utc_now()
            task.result = result
            task.evidence_ids = list(result.evidence_ids)
            task.error = None
            _event(
                workspace,
                EventType.AGENT_COMPLETED,
                f"Agent completed: {task.agent_id}",
                {
                    "task_id": task.task_id,
                    "agent_id": task.agent_id,
                    "capability": task.capability,
                    "summary": result.summary,
                },
            )
            logger.info(
                "Completed mission=%s task=%s agent=%s outcome=success",
                task.mission_id,
                task.task_id,
                task.agent_id,
            )
            await workspace.persist()

    async def _fail(
        self, task: SpecialistTask, workspace: MissionWorkspace, error: str
    ) -> None:
        async with workspace.lock:
            task.error = error
            task.completed_at = utc_now()
            if task.attempt_count < task.max_attempts:
                task.status = StepStatus.PENDING
                task.started_at = None
                retryable = True
            else:
                task.status = StepStatus.FAILED
                retryable = False
            _event(
                workspace,
                EventType.AGENT_FAILED,
                f"Agent failed: {task.agent_id}",
                {
                    "task_id": task.task_id,
                    "agent_id": task.agent_id,
                    "capability": task.capability,
                    "error": error,
                    "retryable": retryable,
                    "attempt_count": task.attempt_count,
                    "max_attempts": task.max_attempts,
                    "critical": task.critical,
                },
            )
            if not retryable and not task.critical:
                _event(
                    workspace,
                    EventType.TASK_SKIPPED,
                    f"Non-critical task skipped after failures: {task.capability}",
                    {"task_id": task.task_id, "error": error},
                )
            await workspace.persist()


def _event(
    workspace: MissionWorkspace,
    event_type: EventType,
    message: str,
    metadata: dict | None = None,
) -> None:
    workspace.mission.events.append(
        MissionEvent(type=event_type, message=message, metadata=metadata or {})
    )
    workspace.mission.touch()
