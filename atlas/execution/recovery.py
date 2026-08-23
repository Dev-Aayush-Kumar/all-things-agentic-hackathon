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
    """

    def __init__(
        self,
        repository: MissionRepository,
        dispatcher: MissionDispatcher,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher

    async def recover(self) -> RecoveryResult:
        result = RecoveryResult()
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
