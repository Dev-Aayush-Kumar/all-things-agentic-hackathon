"""Durable mission claim, lease, and ownership tests."""

import asyncio
from datetime import timedelta

import pytest

from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.exceptions import StaleExecutionError
from atlas.domain.models import Mission, MissionEvent, MissionExecution, utc_now
from atlas.execution.context import ExecutionContext
from atlas.persistence.sqlite_repository import SQLiteMissionRepository


def _queued(goal: str = "Investigate the dataset", max_attempts: int = 3) -> Mission:
    mission = Mission(
        goal=goal,
        execution=MissionExecution(
            state=ExecutionState.QUEUED, max_attempts=max_attempts
        ),
    )
    mission.events.append(
        MissionEvent(type=EventType.MISSION_CREATED, message="Mission created")
    )
    mission.events.append(
        MissionEvent(type=EventType.MISSION_QUEUED, message="Mission queued")
    )
    return mission


async def _expire(repo: SQLiteMissionRepository, mission_id: str) -> None:
    loaded = await repo.get(mission_id)
    assert loaded is not None
    loaded.execution.lease_expires_at = utc_now() - timedelta(seconds=30)
    await repo.update(loaded)


@pytest.mark.asyncio
async def test_created_mission_is_durably_queued(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.CREATED
    assert loaded.execution.state == ExecutionState.QUEUED
    assert loaded.execution.attempt_count == 0
    assert loaded.execution.is_claimed() is False


@pytest.mark.asyncio
async def test_worker_can_claim_queued_mission(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(
        mission.mission_id, "worker-a", lease_seconds=30
    )
    assert claimed is not None
    assert claimed.execution.state == ExecutionState.CLAIMED
    assert claimed.execution.worker_id == "worker-a"
    assert claimed.execution.attempt_count == 1
    assert claimed.execution.is_claimed() is True
    assert any(event.type == EventType.MISSION_CLAIMED for event in claimed.events)


@pytest.mark.asyncio
async def test_second_worker_cannot_claim_same_mission(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    first = await repo.claim(mission.mission_id, "worker-a", lease_seconds=30)
    second = await repo.claim(mission.mission_id, "worker-b", lease_seconds=30)
    assert first is not None
    assert second is None
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.execution.worker_id == "worker-a"


@pytest.mark.asyncio
async def test_concurrent_claims_have_a_single_winner(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    first, second = await asyncio.gather(
        repo.claim(mission.mission_id, "worker-a", lease_seconds=30),
        repo.claim(mission.mission_id, "worker-b", lease_seconds=30),
    )
    winners = [item for item in (first, second) if item is not None]
    assert len(winners) == 1
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.execution.attempt_count == 1
    assert loaded.execution.worker_id in {"worker-a", "worker-b"}


@pytest.mark.asyncio
async def test_ownership_is_validated_on_update(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    owner = ExecutionContext(claimed.execution.execution_id or "", "alice")
    claimed.events.append(
        MissionEvent(type=EventType.PLANNING_STARTED, message="Planning started")
    )
    await repo.update_owned(claimed, owner)

    intruder = ExecutionContext(claimed.execution.execution_id or "", "bob")
    with pytest.raises(StaleExecutionError):
        await repo.update_owned(claimed, intruder)

    stale = ExecutionContext("not-this-execution", "alice")
    with pytest.raises(StaleExecutionError):
        await repo.update_owned(claimed, stale)


@pytest.mark.asyncio
async def test_completed_mission_cannot_be_reclaimed(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    owner = ExecutionContext(claimed.execution.execution_id or "", "alice")
    claimed.status = MissionStatus.COMPLETED
    claimed.execution.state = ExecutionState.COMPLETED
    claimed.completed_at = utc_now()
    await repo.update_owned(claimed, owner)

    again = await repo.claim(mission.mission_id, "bob", lease_seconds=30)
    assert again is None
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.COMPLETED
    assert loaded.execution.state == ExecutionState.COMPLETED


@pytest.mark.asyncio
async def test_expired_lease_is_claimable_by_another_worker(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    first = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert first is not None
    await _expire(repo, mission.mission_id)
    second = await repo.claim(mission.mission_id, "bob", lease_seconds=30)
    assert second is not None
    assert second.execution.worker_id == "bob"
    assert second.execution.attempt_count == 2


@pytest.mark.asyncio
async def test_renew_lease_rejects_non_owner(test_env) -> None:
    repo = SQLiteMissionRepository(test_env)
    mission = _queued()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "alice", lease_seconds=30)
    assert claimed is not None
    owner = ExecutionContext(claimed.execution.execution_id or "", "alice")
    other = ExecutionContext(claimed.execution.execution_id or "", "bob")
    assert await repo.renew_lease(mission.mission_id, owner, lease_seconds=30) is True
    assert await repo.renew_lease(mission.mission_id, other, lease_seconds=30) is False


@pytest.mark.asyncio
async def test_http_mission_records_queue_claim_and_execution_metadata(
    client,
) -> None:
    from httpx import AsyncClient

    from tests.conftest import wait_for_mission_status

    assert isinstance(client, AsyncClient)
    created = await client.post(
        "/missions",
        json={"goal": "Analyze dataset quality issues."},
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED", "FAILED"}
    )
    assert final["status"] == "COMPLETED"
    execution = final["execution"]
    assert execution["state"] == "COMPLETED"
    assert execution["claimed"] is False
    assert execution["attempt_count"] >= 1
    event_types = [event["type"] for event in final["events"]]
    assert "MISSION_QUEUED" in event_types
    assert "MISSION_CLAIMED" in event_types
    assert "EXECUTION_STARTED" in event_types
    assert "MISSION_COMPLETED" in event_types
