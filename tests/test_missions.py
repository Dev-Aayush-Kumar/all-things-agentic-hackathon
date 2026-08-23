"""Mission lifecycle tests."""

import asyncio

import pytest
from httpx import AsyncClient

from tests.conftest import wait_for_mission_status


@pytest.mark.asyncio
async def test_create_mission_accepts_valid_goal(client: AsyncClient) -> None:
    response = await client.post(
        "/missions",
        json={"goal": "Analyze the provided dataset and identify major inconsistencies."},
    )
    assert response.status_code == 202
    payload = response.json()
    assert "mission_id" in payload
    assert payload["status"] == "CREATED"
    assert "created_at" in payload


@pytest.mark.asyncio
async def test_create_mission_rejects_empty_goal(client: AsyncClient) -> None:
    response = await client.post("/missions", json={"goal": ""})
    assert response.status_code == 422

    response = await client.post("/missions", json={"goal": "   "})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_mission_returns_before_workflow_complete(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/missions",
        json={"goal": "Analyze dataset for inconsistencies."},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "CREATED"

    # Immediately after creation, mission should not yet be completed
    detail = await client.get(f"/missions/{payload['mission_id']}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["status"] in {"CREATED", "PLANNING", "EXECUTING"}


@pytest.mark.asyncio
async def test_get_mission_retrieves_mission(client: AsyncClient) -> None:
    create_response = await client.post(
        "/missions",
        json={"goal": "Review system logs and summarize anomalies."},
    )
    mission_id = create_response.json()["mission_id"]

    response = await client.get(f"/missions/{mission_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mission_id"] == mission_id
    assert payload["goal"] == "Review system logs and summarize anomalies."
    assert payload["status"] in {"CREATED", "PLANNING", "EXECUTING", "COMPLETED"}


@pytest.mark.asyncio
async def test_mission_lifecycle_transitions(client: AsyncClient) -> None:
    create_response = await client.post(
        "/missions",
        json={"goal": "Analyze the provided dataset and identify inconsistencies."},
    )
    mission_id = create_response.json()["mission_id"]

    final_mission = await wait_for_mission_status(
        client, mission_id, {"COMPLETED", "FAILED"}
    )
    assert final_mission["status"] == "COMPLETED"

    event_types = [event["type"] for event in final_mission["events"]]
    assert "MISSION_CREATED" in event_types
    assert "MISSION_QUEUED" in event_types
    assert "MISSION_CLAIMED" in event_types
    assert "PLANNING_STARTED" in event_types
    assert "EXECUTION_PLAN_GENERATED" in event_types
    assert "EXECUTION_STARTED" in event_types
    assert "STEP_COMPLETED" in event_types
    assert "MISSION_COMPLETED" in event_types


@pytest.mark.asyncio
async def test_execution_plan_is_generated(client: AsyncClient) -> None:
    create_response = await client.post(
        "/missions",
        json={"goal": "Analyze the provided dataset and identify inconsistencies."},
    )
    mission_id = create_response.json()["mission_id"]

    final_mission = await wait_for_mission_status(client, mission_id, {"COMPLETED"})
    plan = final_mission["execution_plan"]
    assert plan is not None
    assert plan["planner_source"] == "LOCAL_FALLBACK"
    assert len(plan["steps"]) >= 3
    assert all(step["status"] == "COMPLETED" for step in plan["steps"])


@pytest.mark.asyncio
async def test_events_are_recorded_in_order(client: AsyncClient) -> None:
    create_response = await client.post(
        "/missions",
        json={"goal": "Analyze dataset quality issues."},
    )
    mission_id = create_response.json()["mission_id"]

    final_mission = await wait_for_mission_status(client, mission_id, {"COMPLETED"})
    events = final_mission["events"]
    assert len(events) >= 5

    timestamps = [event["timestamp"] for event in events]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_unknown_mission_returns_404(client: AsyncClient) -> None:
    response = await client.get("/missions/nonexistent-mission-id")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_multiple_missions_run_independently(client: AsyncClient) -> None:
    goals = [
        "Analyze dataset A for inconsistencies.",
        "Analyze dataset B for missing values.",
    ]
    mission_ids = []
    for goal in goals:
        response = await client.post("/missions", json={"goal": goal})
        assert response.status_code == 202
        mission_ids.append(response.json()["mission_id"])

    completed = await asyncio.gather(
        *[
            wait_for_mission_status(client, mission_id, {"COMPLETED"})
            for mission_id in mission_ids
        ]
    )
    assert all(m["status"] == "COMPLETED" for m in completed)
    assert completed[0]["goal"] != completed[1]["goal"]
