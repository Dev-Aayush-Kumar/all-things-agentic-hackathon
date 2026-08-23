"""Mission worker: claim, run existing workflow, maintain lease, finalize."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from atlas.config.settings import Settings
from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.exceptions import StaleExecutionError
from atlas.domain.models import MissionEvent
from atlas.execution.context import ExecutionContext
from atlas.persistence.base import MissionRepository
from atlas.workflow.mission_runner import MissionWorkflowRunner

logger = logging.getLogger(__name__)


def _consume_heartbeat_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.debug("Heartbeat task ended with error: %s", exception)


class MissionWorker:
    """Owns claimed executions and runs the existing mission workflow."""

    def __init__(
        self,
        repository: MissionRepository,
        workflow_runner: MissionWorkflowRunner,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._workflow_runner = workflow_runner
        self._settings = settings
        self.worker_id = worker_id or settings.worker_id or f"local-{uuid4().hex[:8]}"

    async def execute(self, mission_id: str) -> None:
        """Claim a mission if possible and run its workflow under that lease."""
        claimed = await self._repository.claim(
            mission_id,
            self.worker_id,
            lease_seconds=self._settings.execution_lease_seconds,
        )
        if claimed is None:
            logger.info("Mission %s was not claimed by %s", mission_id, self.worker_id)
            return

        assert claimed.execution.execution_id is not None
        context = ExecutionContext(
            execution_id=claimed.execution.execution_id,
            worker_id=self.worker_id,
        )
        claimed.execution.state = ExecutionState.RUNNING
        try:
            await self._repository.update_owned(claimed, context)
        except StaleExecutionError:
            logger.warning("Lost lease before workflow start for %s", mission_id)
            return

        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(mission_id, context, stop),
            name=f"atlas-heartbeat-{mission_id}",
        )
        heartbeat.add_done_callback(_consume_heartbeat_result)
        try:
            await self._workflow_runner.run(mission_id, context)
        except StaleExecutionError:
            logger.warning("Worker %s lost ownership of %s", self.worker_id, mission_id)
        except Exception:
            logger.exception("Worker %s failed while executing %s", self.worker_id, mission_id)
            await self._record_lost_attempt(mission_id, context)
        finally:
            stop.set()
            heartbeat.cancel()

    async def _heartbeat(
        self,
        mission_id: str,
        context: ExecutionContext,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.05, self._settings.execution_heartbeat_seconds)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except (TimeoutError, asyncio.TimeoutError):
                if stop.is_set():
                    return
                renewed = await self._repository.renew_lease(
                    mission_id,
                    context,
                    lease_seconds=self._settings.execution_lease_seconds,
                )
                if not renewed:
                    logger.warning("Heartbeat failed for mission %s", mission_id)
                    return

    async def _record_lost_attempt(
        self,
        mission_id: str,
        context: ExecutionContext,
    ) -> None:
        mission = await self._repository.get(mission_id)
        if mission is None:
            return
        if mission.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}:
            return
        mission.events.append(
            MissionEvent(
                type=EventType.ATTEMPT_FAILED,
                message="Worker attempt failed unexpectedly",
                metadata={
                    "worker_id": context.worker_id,
                    "execution_id": context.execution_id,
                    "attempt_count": mission.execution.attempt_count,
                },
            )
        )
        mission.execution.last_error = "Worker attempt failed unexpectedly"
        try:
            await self._repository.update_owned(mission, context)
        except StaleExecutionError:
            return
