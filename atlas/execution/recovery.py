"""Bounded recovery of abandoned mission executions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from atlas.execution.dispatcher import MissionDispatcher
from atlas.persistence.base import MissionRepository

logger = logging.getLogger(__name__)


@dataclass
class RecoveryResult:
    """Outcome of a recovery pass."""

    recovered_mission_ids: list[str] = field(default_factory=list)
    exhausted_mission_ids: list[str] = field(default_factory=list)
    skipped_mission_ids: list[str] = field(default_factory=list)


class MissionRecoveryService:
    """Requeue or exhaust missions whose worker leases have expired.

    Invoke explicitly (tests, local ops). Not a standing scheduler.
    Waiting-for-approval missions are not recovered as abandoned workers.
    Expired approvals are marked and requeued for replanning.
    """

    def __init__(
        self,
        repository: MissionRepository,
        dispatcher: MissionDispatcher,
        approval_repository=None,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._approval_repository = approval_repository

    async def recover(self) -> RecoveryResult:
        result = RecoveryResult()
        await self._expire_approvals()
        abandoned = await self._repository.list_recoverable()
        for mission in abandoned:
            if mission.execution.attempt_count >= mission.execution.max_attempts:
                exhausted = await self._repository.exhaust_expired(mission.mission_id)
                if exhausted is not None:
                    result.exhausted_mission_ids.append(mission.mission_id)
                    logger.info("Exhausted mission %s", mission.mission_id)
                else:
                    result.skipped_mission_ids.append(mission.mission_id)
                continue

            requeued = await self._repository.requeue_expired(mission.mission_id)
            if requeued is None:
                result.skipped_mission_ids.append(mission.mission_id)
                continue
            await self._dispatcher.dispatch(mission.mission_id)
            result.recovered_mission_ids.append(mission.mission_id)
            logger.info("Recovered mission %s", mission.mission_id)
        return result

    async def _expire_approvals(self) -> None:
        if self._approval_repository is None:
            return
        from atlas.services.approval_service import ApprovalService

        service = ApprovalService(
            self._approval_repository,
            self._repository,
            self._dispatcher,
        )
        await service.expire_due()
