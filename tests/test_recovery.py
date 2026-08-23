"""Recovery of abandoned mission executions."""

from datetime import timedelta

import pytest
from httpx import AsyncClient

from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.models import Mission, MissionEvent, MissionExecution, utc_now
from atlas.execution.context import ExecutionContext
from atlas.execution.recovery import MissionRecoveryService
from atlas.persistence.sqlite_repository import SQLiteMissionRepository
from tests.conftest import wait_for_mission_status


class _RecordingDispatcher:
    backend_name = "local_async"

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def dispatch(self, mission_id: str) -> None:
        self.dispatched.append(mission_id)


def _queued(max_attempts: int = 3) -> Mission:
    mission = Mission(
        goal="Investigate the dataset",
        execution=MissionExecution(
            state=ExecutionState.QUEUED, max_attempts=max_attempts
        ),
    )
    mission.events.append(
        MissionEvent(type=EventType.MISSION_CREATED, message="Mission created")
    )
    return mission


async def _expire(repo: SQLiteMissionRepository, mission_id: str) -> None:
    loaded = await repo.get(mission_id)
    assert loaded is not None
    loaded.execution.lease_expires_at = utc_now() - timedelta(seconds=30)
    await repo.update(loaded)


@pytest.mark.asyncio
async def test_expired_lease_is_recoverable_and_requeued(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    claimed.status = MissionStatus.EXECUTING
    await repo.update_owned(
        claimed,
        ExecutionContext(claimed.execution.execution_id or "", "alice"),
    )
    await _expire(repo, mission.mission_id)

    dispatcher = _RecordingDispatcher()
    recovery = MissionRecoveryService(repo, dispatcher)
    result = await recovery.recover()
    assert mission.mission_id in result.recovered_mission_ids
    assert dispatcher.dispatched == [mission.mission_id]

    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.EXECUTING
    assert loaded.execution.state == ExecutionState.QUEUED
    assert loaded.execution.worker_id is None
    event_types = [event.type for event in loaded.events]
    assert EventType.LEASE_EXPIRED in event_types
    assert EventType.MISSION_RECOVERED in event_types


@pytest.mark.asyncio
async def test_recovery_respects_max_attempts(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued(max_attempts=1)
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    await _expire(repo, mission.mission_id)

    dispatcher = _RecordingDispatcher()
    recovery = MissionRecoveryService(repo, dispatcher)
    result = await recovery.recover()
    assert mission.mission_id in result.exhausted_mission_ids
    assert dispatcher.dispatched == []

    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.FAILED
    assert loaded.execution.state == ExecutionState.EXHAUSTED
    assert loaded.execution.attempt_count == 1
    event_types = [event.type for event in loaded.events]
    assert EventType.EXECUTION_EXHAUSTED in event_types
    assert EventType.MISSION_FAILED in event_types

    again = await recovery.recover()
    assert mission.mission_id not in again.recovered_mission_ids
    assert mission.mission_id not in again.exhausted_mission_ids
    assert await repo.claim(mission.mission_id, "bob", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_recovery_never_resurrects_completed_missions(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    claimed.status = MissionStatus.COMPLETED
    claimed.execution.state = ExecutionState.COMPLETED
    claimed.completed_at = utc_now()
    await repo.update_owned(
        claimed,
        ExecutionContext(claimed.execution.execution_id or "", "alice"),
    )
    await _expire(repo, mission.mission_id)

    dispatcher = _RecordingDispatcher()
    result = await MissionRecoveryService(repo, dispatcher).recover()
    assert mission.mission_id not in result.recovered_mission_ids
    assert mission.mission_id not in result.exhausted_mission_ids
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.COMPLETED
    assert await repo.claim(mission.mission_id, "bob", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_recovery_never_resurrects_failed_missions(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    claimed.status = MissionStatus.FAILED
    claimed.execution.state = ExecutionState.FAILED
    claimed.error = "parse failed"
    await repo.update_owned(
        claimed,
        ExecutionContext(claimed.execution.execution_id or "", "alice"),
    )
    await _expire(repo, mission.mission_id)

    dispatcher = _RecordingDispatcher()
    result = await MissionRecoveryService(repo, dispatcher).recover()
    assert result.recovered_mission_ids == []
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.FAILED


@pytest.mark.asyncio
async def test_http_recovery_endpoint_and_completed_mission_events(
    client: AsyncClient,
) -> None:
    created = await client.post(
        "/missions",
        json={"goal": "Analyze dataset quality issues."},
    )
    mission_id = created.json()["mission_id"]
    final = await wait_for_mission_status(client, mission_id, {"COMPLETED"})
    started = [
        event for event in final["events"] if event["type"] == "EXECUTION_STARTED"
    ]
    assert len(started) == 1
    assert final["execution"]["state"] == "COMPLETED"
    assert final["execution"]["claimed"] is False

    recovered = await client.post("/ops/recover-missions")
    assert recovered.status_code == 200
    payload = recovered.json()
    assert mission_id not in payload["recovered_mission_ids"]
    assert mission_id not in payload["exhausted_mission_ids"]

    after = await client.get(f"/missions/{mission_id}")
    assert after.json()["status"] == "COMPLETED"
    started_after = [
        event
        for event in after.json()["events"]
        if event["type"] == "EXECUTION_STARTED"
    ]
    assert len(started_after) == 1
