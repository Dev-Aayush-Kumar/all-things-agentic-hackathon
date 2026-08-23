"""Idempotent mission creation tests."""

import pytest
from httpx import AsyncClient

from tests.conftest import wait_for_mission_status


@pytest.mark.asyncio
async def test_idempotency_key_creates_one_mission(client: AsyncClient) -> None:
    payload = {
        "goal": "Analyze dataset quality issues.",
        "idempotency_key": "client-req-1",
    }
    first = await client.post("/missions", json=payload)
    second = await client.post("/missions", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["mission_id"] == second.json()["mission_id"]

    final = await wait_for_mission_status(
        client, first.json()["mission_id"], {"COMPLETED", "FAILED"}
    )
    assert final["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_idempotency_key_conflict_on_different_payload(
    client: AsyncClient,
) -> None:
    first = await client.post(
        "/missions",
        json={
            "goal": "Analyze dataset quality issues.",
            "idempotency_key": "client-req-2",
        },
    )
    assert first.status_code == 202
    conflict = await client.post(
        "/missions",
        json={
            "goal": "A completely different mission goal.",
            "idempotency_key": "client-req-2",
        },
    )
    assert conflict.status_code == 409
    assert "different" in conflict.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_idempotency_key_creates_distinct_missions(
    client: AsyncClient,
) -> None:
    payload = {"goal": "Analyze dataset quality issues."}
    first = await client.post("/missions", json=payload)
    second = await client.post("/missions", json=payload)
    assert first.json()["mission_id"] != second.json()["mission_id"]
