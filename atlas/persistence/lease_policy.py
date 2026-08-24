"""Eligibility rules for claiming, owning, and recovering mission executions."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.models import Mission, MissionEvent, utc_now
from atlas.execution.context import ExecutionContext


TERMINAL_LIFECYCLE = {MissionStatus.COMPLETED, MissionStatus.FAILED}
TERMINAL_EXECUTION = {
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.EXHAUSTED,
}
OWNED_STATES = {ExecutionState.CLAIMED, ExecutionState.RUNNING}


def is_claimable(mission: Mission, now: datetime) -> bool:
    """Whether a worker may claim this mission right now."""
    if mission.status in TERMINAL_LIFECYCLE:
        return False
    if mission.execution.state in TERMINAL_EXECUTION:
        return False
    if mission.execution.state == ExecutionState.WAITING_FOR_APPROVAL:
        return False
    if mission.status == MissionStatus.WAITING_FOR_APPROVAL:
        return False
    if mission.execution.attempt_count >= mission.execution.max_attempts:
        return False
    if mission.execution.state == ExecutionState.QUEUED:
        return True
    if mission.execution.state in OWNED_STATES:
        expires = mission.execution.lease_expires_at
        return expires is not None and expires <= now
    return False


def is_recoverable(mission: Mission, now: datetime) -> bool:
    """Incomplete mission whose worker lease has expired."""
    if mission.status in TERMINAL_LIFECYCLE:
        return False
    if mission.execution.state in TERMINAL_EXECUTION:
        return False
    if mission.execution.state == ExecutionState.WAITING_FOR_APPROVAL:
        return False
    if mission.status == MissionStatus.WAITING_FOR_APPROVAL:
        return False
    if mission.execution.state == ExecutionState.QUEUED:
        return False
    expires = mission.execution.lease_expires_at
    return expires is not None and expires <= now


def is_owned(mission: Mission, context: ExecutionContext, now: datetime) -> bool:
    execution = mission.execution
    if execution.state not in OWNED_STATES:
        return False
    if execution.execution_id != context.execution_id:
        return False
    if execution.worker_id != context.worker_id:
        return False
    expires = execution.lease_expires_at
    return expires is not None and expires > now


def apply_claim(
    mission: Mission,
    worker_id: str,
    *,
    lease_seconds: float,
    now: datetime | None = None,
) -> Mission:
    current = now or utc_now()
    execution_id = str(uuid4())
    expires = current + timedelta(seconds=lease_seconds)
    mission.execution.state = ExecutionState.CLAIMED
    mission.execution.execution_id = execution_id
    mission.execution.worker_id = worker_id
    mission.execution.claimed_at = current
    mission.execution.lease_expires_at = expires
    mission.execution.heartbeat_at = current
    if mission.execution.resume_without_attempt:
        mission.execution.resume_without_attempt = False
    else:
        mission.execution.attempt_count += 1
    mission.execution.last_error = None
    mission.events.append(
        MissionEvent(
            type=EventType.MISSION_CLAIMED,
            message="Mission claimed by worker",
            metadata={
                "worker_id": worker_id,
                "execution_id": execution_id,
                "attempt_count": mission.execution.attempt_count,
            },
        )
    )
    mission.touch()
    return mission
