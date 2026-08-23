"""Pub/Sub message and worker validation tests. No live Pub/Sub."""

import base64
import json

import pytest
from httpx import AsyncClient

from atlas.domain.enums import ExecutionState, MissionStatus
from atlas.domain.exceptions import MissionNotExecutableError
from atlas.domain.models import Mission
from atlas.execution.pubsub_handler import handle_mission_message
from atlas.execution.pubsub_messages import (
    encode_execution_message,
    parse_push_envelope,
)
from atlas.persistence.firestore_repository import FirestoreMissionRepository
from atlas.persistence.memory_store import MemoryDocumentStore


class RecordingWorker:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, mission_id: str) -> None:
        self.executed.append(mission_id)


@pytest.mark.asyncio
async def test_push_envelope_from_pubsub_body() -> None:
    raw = encode_execution_message("mission-9")
    body = {
        "message": {
            "data": base64.b64encode(raw).decode("ascii"),
            "attributes": {"mission_id": "mission-9", "source": "atlas"},
        }
    }
    assert parse_push_envelope(body) == "mission-9"


def test_push_envelope_direct_mission_id() -> None:
    assert parse_push_envelope({"mission_id": "mission-9"}) == "mission-9"


def test_push_envelope_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_push_envelope({"message": {}})


@pytest.mark.asyncio
async def test_worker_executes_queued_mission() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = Mission(goal="Investigate dataset quality")
    await repo.create(mission)
    worker = RecordingWorker()
    outcome = await handle_mission_message(
        mission.mission_id, repository=repo, worker=worker  # type: ignore[arg-type]
    )
    assert outcome == "executed"
    assert worker.executed == [mission.mission_id]


@pytest.mark.asyncio
async def test_worker_ignores_completed_mission() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = Mission(goal="done")
    mission.status = MissionStatus.COMPLETED
    mission.execution.state = ExecutionState.COMPLETED
    await repo.create(mission)
    worker = RecordingWorker()
    outcome = await handle_mission_message(
        mission.mission_id, repository=repo, worker=worker  # type: ignore[arg-type]
    )
    assert outcome == "ignored_terminal"
    assert worker.executed == []


@pytest.mark.asyncio
async def test_worker_rejects_missing_mission() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    worker = RecordingWorker()
    with pytest.raises(MissionNotExecutableError, match="does not exist"):
        await handle_mission_message(
            "missing-id", repository=repo, worker=worker  # type: ignore[arg-type]
        )
    assert worker.executed == []


@pytest.mark.asyncio
async def test_worker_ignores_attempt_limit() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = Mission(goal="exhausted")
    mission.execution.attempt_count = mission.execution.max_attempts
    await repo.create(mission)
    worker = RecordingWorker()
    outcome = await handle_mission_message(
        mission.mission_id, repository=repo, worker=worker  # type: ignore[arg-type]
    )
    assert outcome == "ignored_attempts"
    assert worker.executed == []


@pytest.mark.asyncio
async def test_worker_does_not_steal_valid_lease() -> None:
    repo = FirestoreMissionRepository(MemoryDocumentStore())
    mission = Mission(goal="running")
    await repo.create(mission)
    claimed = await repo.claim(mission.mission_id, "owner", lease_seconds=60)
    assert claimed is not None
    worker = RecordingWorker()
    outcome = await handle_mission_message(
        mission.mission_id, repository=repo, worker=worker  # type: ignore[arg-type]
    )
    assert outcome == "not_claimable"
    assert worker.executed == []


@pytest.mark.asyncio
async def test_pubsub_push_endpoint_rejects_invalid_payload(client: AsyncClient) -> None:
    response = await client.post("/internal/pubsub/push", json={"message": {}})
    assert response.status_code == 400
    assert "secret" not in response.text.lower()


@pytest.mark.asyncio
async def test_encode_message_is_small_and_has_no_secrets() -> None:
    raw = encode_execution_message("abc")
    payload = json.loads(raw.decode("utf-8"))
    assert set(payload) == {"mission_id", "source"}
    assert b"GOOGLE" not in raw
    assert b"key" not in raw
