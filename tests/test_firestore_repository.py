"""Firestore repository tests using an in-memory document store.

No Google Cloud credentials or network calls.
"""

from datetime import timedelta

import pytest

from atlas.domain.enums import ExecutionState, MissionStatus
from atlas.domain.exceptions import IdempotencyConflictError, StaleExecutionError
from atlas.domain.models import (
    DatasetRecord,
    ExecutionPlan,
    Mission,
    MissionEvent,
    PlanStep,
    utc_now,
)
from atlas.domain.enums import EventType, PlannerSource, StepStatus
from atlas.execution.context import ExecutionContext
from atlas.persistence.codec import document_to_mission, mission_to_document
from atlas.persistence.firestore_repository import (
    FirestoreDatasetRepository,
    FirestoreMissionRepository,
)
from atlas.persistence.memory_store import MemoryDocumentStore


def _queued_mission(goal: str = "Investigate dataset quality") -> Mission:
    return Mission(goal=goal)


@pytest.mark.asyncio
async def test_codec_roundtrip_preserves_mission_state() -> None:
    mission = Mission(
        goal="Investigate missing values",
        dataset_id="ds-1",
        status=MissionStatus.EXECUTING,
        execution_plan=ExecutionPlan(
            steps=[
                PlanStep(
                    id="step_1",
                    title="Profile",
                    description="Profile the dataset",
                    status=StepStatus.PENDING,
                )
            ],
            planner_source=PlannerSource.LOCAL_FALLBACK,
            summary="Local plan",
        ),
        events=[
            MissionEvent(
                type=EventType.MISSION_CREATED,
                message="Mission created",
                metadata={"goal": "Investigate missing values"},
            )
        ],
    )
    document = mission_to_document(mission)
    assert document["mission_id"] == mission.mission_id
    assert document["goal"] == mission.goal
    assert document["dataset_id"] == "ds-1"
    assert document["status"] == MissionStatus.EXECUTING.value
    assert document["execution_state"] == ExecutionState.QUEUED.value
    assert isinstance(document["payload"], dict)
    restored = document_to_mission(document)
    assert restored.mission_id == mission.mission_id
    assert restored.goal == mission.goal
    assert restored.dataset_id == "ds-1"
    assert restored.status == MissionStatus.EXECUTING
    assert restored.execution_plan is not None
    assert restored.execution_plan.summary == "Local plan"
    assert restored.events[0].type == EventType.MISSION_CREATED


@pytest.mark.asyncio
async def test_firestore_repository_satisfies_create_get_update() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = _queued_mission()
    await repo.create(mission)
    loaded = await repo.get(mission.mission_id)
    assert loaded is not None
    assert loaded.goal == mission.goal
    loaded.status = MissionStatus.PLANNING
    await repo.update(loaded)
    again = await repo.get(mission.mission_id)
    assert again is not None
    assert again.status == MissionStatus.PLANNING


@pytest.mark.asyncio
async def test_firestore_claim_is_exclusive() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = _queued_mission()
    await repo.create(mission)
    first = await repo.claim(mission.mission_id, "worker-a", lease_seconds=30)
    second = await repo.claim(mission.mission_id, "worker-b", lease_seconds=30)
    assert first is not None
    assert first.execution.state == ExecutionState.CLAIMED
    assert first.execution.worker_id == "worker-a"
    assert first.execution.attempt_count == 1
    assert second is None


@pytest.mark.asyncio
async def test_firestore_owned_update_rejects_other_worker() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = _queued_mission()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.execution.execution_id is not None
    claimed.status = MissionStatus.EXECUTING
    claimed.execution.state = ExecutionState.RUNNING
    updated = await repo.update_owned(
        claimed,
        ExecutionContext(
            execution_id=claimed.execution.execution_id,
            worker_id="worker-a",
        ),
    )
    assert updated.status == MissionStatus.EXECUTING
    with pytest.raises(StaleExecutionError):
        await repo.update_owned(
            claimed,
            ExecutionContext(execution_id="other", worker_id="worker-b"),
        )


@pytest.mark.asyncio
async def test_firestore_completed_mission_cannot_be_reclaimed() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = _queued_mission()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "worker-a", lease_seconds=30)
    assert claimed is not None
    claimed.status = MissionStatus.COMPLETED
    claimed.execution.state = ExecutionState.COMPLETED
    await repo.update_owned(
        claimed,
        ExecutionContext(
            execution_id=claimed.execution.execution_id or "",
            worker_id="worker-a",
        ),
    )
    again = await repo.claim(mission.mission_id, "worker-b", lease_seconds=30)
    assert again is None


@pytest.mark.asyncio
async def test_firestore_idempotency_replays_same_payload() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = _queued_mission("same goal")
    stored, replayed = await repo.create_idempotent(mission, "key-1", "fp-1")
    assert replayed is False
    again, replayed_again = await repo.create_idempotent(
        _queued_mission("same goal"), "key-1", "fp-1"
    )
    assert replayed_again is True
    assert again.mission_id == stored.mission_id


@pytest.mark.asyncio
async def test_firestore_idempotency_conflict() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    await repo.create_idempotent(_queued_mission("goal a"), "key-1", "fp-a")
    with pytest.raises(IdempotencyConflictError):
        await repo.create_idempotent(_queued_mission("goal b"), "key-1", "fp-b")


@pytest.mark.asyncio
async def test_firestore_dataset_repository_roundtrip() -> None:
    repo = FirestoreDatasetRepository(MemoryDocumentStore())
    record = DatasetRecord(
        original_filename="orders.csv",
        stored_filename="abc.csv",
        content_type="text/csv",
        size_bytes=12,
    )
    await repo.create(record)
    loaded = await repo.get(record.dataset_id)
    assert loaded is not None
    assert loaded.original_filename == "orders.csv"
    assert loaded.stored_filename == "abc.csv"


@pytest.mark.asyncio
async def test_firestore_list_recoverable_expired_lease() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = _queued_mission()
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "worker-a", lease_seconds=1)
    assert claimed is not None
    claimed.execution.lease_expires_at = utc_now() - timedelta(seconds=5)
    await repo.update(claimed)
    recoverable = await repo.list_recoverable()
    assert [item.mission_id for item in recoverable] == [mission.mission_id]
