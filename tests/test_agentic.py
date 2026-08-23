"""HTTP tests for agentic dataset missions."""

import pytest
from httpx import AsyncClient

from tests.conftest import FIXTURES_DIR, wait_for_mission_status

SURVEY = FIXTURES_DIR / "survey_quality.csv"
CLEAN = FIXTURES_DIR / "clean_numeric.csv"
HEAVY = FIXTURES_DIR / "missing_heavy.csv"


async def _upload(client: AsyncClient, path, name: str) -> str:
    response = await client.post(
        "/datasets",
        files={"file": (name, path.read_bytes(), "text/csv")},
    )
    assert response.status_code == 201
    return response.json()["dataset_id"]


@pytest.mark.asyncio
async def test_mission_produces_structured_agent_plan(client: AsyncClient) -> None:
    dataset_id = await _upload(client, SURVEY, "survey_quality.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": (
                "Analyze this survey dataset, identify the most important quality "
                "problems, investigate what may be causing them, and tell me what "
                "should be fixed first."
            ),
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED", "FAILED"}
    )
    assert final["status"] == "COMPLETED"
    plan = final["agent_plan"]
    assert plan is not None
    assert plan["objective"]
    assert "profile_dataset" in plan["selected_tools"]
    assert len(plan["tasks"]) >= 2
    assert all("tool_name" in task for task in plan["tasks"])
    assert all(task["status"] == "COMPLETED" for task in plan["tasks"])
    assert plan["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_tools_return_and_preserve_evidence(client: AsyncClient) -> None:
    dataset_id = await _upload(client, SURVEY, "survey_quality.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": "Analyze quality problems and inconsistencies in this dataset.",
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    assert final["tool_invocations"]
    assert all(item["tool_name"] for item in final["tool_invocations"])
    assert final["evidence_records"]
    for record in final["evidence_records"]:
        assert record["observed_facts"]
        assert record["tool_name"]
    report = final["investigation_report"]
    assert report["findings"]
    for finding in report["findings"]:
        assert finding["evidence"]
        assert finding["detection_method"]


@pytest.mark.asyncio
async def test_interpretation_is_distinct_from_evidence(client: AsyncClient) -> None:
    dataset_id = await _upload(client, SURVEY, "survey_quality.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": "Analyze quality problems in this dataset.",
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    assert final["interpretations"]
    finding_ids = {item["finding_id"] for item in final["investigation_report"]["findings"]}
    for interpretation in final["interpretations"]:
        assert interpretation["kind"]
        assert interpretation["text"]
        assert interpretation["related_evidence_ids"]
        assert set(interpretation["related_finding_ids"]).issubset(finding_ids)
    evidence_ids = {item["evidence_id"] for item in final["evidence_records"]}
    for interpretation in final["interpretations"]:
        assert set(interpretation["related_evidence_ids"]).issubset(evidence_ids)


@pytest.mark.asyncio
async def test_adaptive_branch_from_extreme_profile(client: AsyncClient) -> None:
    dataset_id = await _upload(client, SURVEY, "survey_quality.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": "Check only for duplicate rows in this CSV.",
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    assert final["status"] == "COMPLETED"
    initial_tools = None
    for event in final["events"]:
        if event["type"] == "AGENT_PLAN_CREATED":
            initial_tools = event["metadata"]["selected_tools"]
            break
    assert initial_tools is not None
    assert "analyze_duplicates" in initial_tools
    assert "analyze_outliers" not in initial_tools

    event_types = [event["type"] for event in final["events"]]
    assert "ADAPTIVE_INVESTIGATION_TRIGGERED" in event_types
    adaptive = [
        event
        for event in final["events"]
        if event["type"] == "ADAPTIVE_INVESTIGATION_TRIGGERED"
    ]
    assert any(event["metadata"]["tool_name"] == "analyze_outliers" for event in adaptive)
    called = [item["tool_name"] for item in final["tool_invocations"]]
    assert "analyze_outliers" in called
    assert any(item["adaptive"] for item in final["tool_invocations"])


@pytest.mark.asyncio
async def test_adaptive_branch_does_not_always_occur(client: AsyncClient) -> None:
    dataset_id = await _upload(client, CLEAN, "clean_numeric.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": "Check only for duplicate rows in this CSV.",
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    assert final["status"] == "COMPLETED"
    event_types = [event["type"] for event in final["events"]]
    assert "ADAPTIVE_INVESTIGATION_TRIGGERED" not in event_types
    called = [item["tool_name"] for item in final["tool_invocations"]]
    assert "analyze_outliers" not in called
    assert "inspect_column" not in called


@pytest.mark.asyncio
async def test_material_missing_triggers_column_inspection(
    client: AsyncClient,
) -> None:
    dataset_id = await _upload(client, HEAVY, "missing_heavy.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": "Check this dataset for missing values.",
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    adaptive = [
        event
        for event in final["events"]
        if event["type"] == "ADAPTIVE_INVESTIGATION_TRIGGERED"
    ]
    assert any(event["metadata"]["tool_name"] == "inspect_column" for event in adaptive)
    inspect_calls = [
        item
        for item in final["tool_invocations"]
        if item["tool_name"] == "inspect_column"
    ]
    assert inspect_calls
    assert inspect_calls[0]["arguments"]["column_name"] == "notes"
    assert inspect_calls[0]["adaptive"] is True


@pytest.mark.asyncio
async def test_agent_operational_events_are_recorded(client: AsyncClient) -> None:
    dataset_id = await _upload(client, SURVEY, "survey_quality.csv")
    created = await client.post(
        "/missions",
        json={
            "goal": "Analyze quality problems in this dataset.",
            "dataset_id": dataset_id,
        },
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    event_types = [event["type"] for event in final["events"]]
    for expected in [
        "MISSION_CREATED",
        "MISSION_UNDERSTOOD",
        "AGENT_PLAN_CREATED",
        "TOOL_SELECTED",
        "TOOL_STARTED",
        "TOOL_COMPLETED",
        "EVIDENCE_RECEIVED",
        "AGENT_DECISION",
        "FINAL_REASONING_COMPLETED",
        "MISSION_COMPLETED",
    ]:
        assert expected in event_types
    assert final["current_phase"] == "COMPLETING"
    assert final["investigation_report"]["reasoning_source"] == "LOCAL_FALLBACK"
