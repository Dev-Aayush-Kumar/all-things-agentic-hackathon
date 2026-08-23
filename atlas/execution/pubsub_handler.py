"""Validate a Pub/Sub mission message, then run the worker.

The message only identifies a durable mission. Authoritative state is loaded
from the mission repository (Firestore in cloud mode).
"""

from __future__ import annotations

import logging

from atlas.domain.enums import ExecutionState, MissionStatus
from atlas.domain.exceptions import MissionNotExecutableError
from atlas.domain.models import utc_now
from atlas.execution.worker import MissionWorker
from atlas.persistence.base import MissionRepository
from atlas.persistence.lease_policy import is_claimable

logger = logging.getLogger(__name__)

TERMINAL_LIFECYCLE = {MissionStatus.COMPLETED, MissionStatus.FAILED}
TERMINAL_EXECUTION = {
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.EXHAUSTED,
}


async def handle_mission_message(
    mission_id: str,
    *,
    repository: MissionRepository,
    worker: MissionWorker,
) -> str:
    """Load, validate, and execute a mission identified by a Pub/Sub message.

    Returns a short outcome token. Raises MissionNotExecutableError for
    malformed/absent missions that should not be retried forever.
    """
    if not mission_id or not mission_id.strip():
        raise MissionNotExecutableError(mission_id or "", "missing mission_id")
    mission_id = mission_id.strip()

    mission = await repository.get(mission_id)
    if mission is None:
        raise MissionNotExecutableError(mission_id, "mission does not exist")

    if mission.status in TERMINAL_LIFECYCLE:
        logger.info("Ignoring terminal mission %s status=%s", mission_id, mission.status.value)
        return "ignored_terminal"

    if mission.execution.state in TERMINAL_EXECUTION:
        logger.info(
            "Ignoring terminal execution %s state=%s",
            mission_id,
            mission.execution.state.value,
        )
        return "ignored_terminal"

    if mission.execution.attempt_count >= mission.execution.max_attempts:
        logger.info(
            "Ignoring mission %s at attempt limit %s",
            mission_id,
            mission.execution.attempt_count,
        )
        return "ignored_attempts"

    now = utc_now()
    if not is_claimable(mission, now):
        logger.info(
            "Mission %s is not currently claimable (state=%s)",
            mission_id,
            mission.execution.state.value,
        )
        return "not_claimable"

    await worker.execute(mission_id)
    return "executed"
