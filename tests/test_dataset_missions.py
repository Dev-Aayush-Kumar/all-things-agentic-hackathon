"""Dataset-backed mission lifecycle tests."""

import pytest
from httpx import AsyncClient

from tests.conftest import FIXTURES_DIR, wait_for_mission_status

SURVEY_CSV = FIXTURES_DIR / "survey_quality.csv"
INVALID_CSV = FIXTURES_DIR / "invalid_header.csv"
GOAL = (
    "Analyze this dataset, identify important data quality problems and "
    "inconsistencies, investigate likely causes, prioritize the issues, "
    "and produce a concrete resolution plan."
)


async def _upload_survey(client: AsyncClient) -> str:
    response = await client.post(
        "/datasets",
        files={"file": ("survey_quality.csv", SURVEY_CSV.read_bytes(), "text/csv")},
    )
    assert response.status_code == 201
    return response.json()["dataset_id"]


@pytest.mark.asyncio
async def test_mission_creation_with_valid_dataset_succeeds(
    client: AsyncClient,
) -> None:
    dataset_id = await _upload_survey(client)
    response = await client.post(
        "/missions",
        json={"goal": GOAL, "dataset_id": dataset_id},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "CREATED"
    assert payload["dataset_id"] == dataset_id


@pytest.mark.asyncio
async def test_unknown_dataset_id_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/missions",
        json={"goal": GOAL, "dataset_id": "does-not-exist"},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_dataset_mission_returns_promptly_and_runs_async(
    client: AsyncClient,
) -> None:
    dataset_id = await _upload_survey(client)
    response = await client.post(
        "/missions",
        json={"goal": GOAL, "dataset_id": dataset_id},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "CREATED"

    detail = await client.get(f"/missions/{response.json()['mission_id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] in {"CREATED", "PLANNING", "EXECUTING"}
    await wait_for_mission_status(
        client, response.json()["mission_id"], {"COMPLETED", "FAILED"}
    )


@pytest.mark.asyncio
async def test_dataset_mission_completes_with_structured_report(
    client: AsyncClient,
) -> None:
    dataset_id = await _upload_survey(client)
    created = await client.post(
        "/missions",
        json={"goal": GOAL, "dataset_id": dataset_id},
    )
    mission_id = created.json()["mission_id"]
    final = await wait_for_mission_status(client, mission_id, {"COMPLETED", "FAILED"})
    assert final["status"] == "COMPLETED"
    assert final["dataset_id"] == dataset_id

    report = final["investigation_report"]
    assert report is not None
    assert report["dataset_summary"]["row_count"] == 11
    assert report["dataset_summary"]["column_count"] == 9
    assert report["dataset_summary"]["original_filename"] == "survey_quality.csv"
    assert report["findings"]
    assert report["recommended_actions"]
    assert report["mission_summary"]
    assert report["investigation_summary"]
    assert report["overall_assessment"]
    assert report["reasoning_source"] == "LOCAL_FALLBACK"

    categories = {finding["category"] for finding in report["findings"]}
    assert "MISSING_DATA" in categories
    assert "DUPLICATE_ROWS" in categories
    assert categories & {
        "TYPE_FORMAT_ANOMALY",
        "NUMERIC_OUTLIER",
        "CONSISTENCY_VIOLATION",
        "CATEGORICAL_INCONSISTENCY",
    }

    for finding in report["findings"]:
        assert finding["evidence"]
        assert finding["priority"] >= 1
        assert finding["detection_method"]

    priorities = [finding["priority"] for finding in report["findings"]]
    assert priorities == sorted(priorities)
    assert all(step["status"] == "COMPLETED" for step in final["execution_plan"]["steps"])


@pytest.mark.asyncio
async def test_dataset_mission_events_reflect_pipeline_stages(
    client: AsyncClient,
) -> None:
    dataset_id = await _upload_survey(client)
    created = await client.post(
        "/missions",
        json={"goal": GOAL, "dataset_id": dataset_id},
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED"}
    )
    event_types = [event["type"] for event in final["events"]]
    for expected in [
        "MISSION_CREATED",
        "PLANNING_STARTED",
        "EXECUTION_PLAN_GENERATED",
        "EXECUTION_STARTED",
        "INVESTIGATION_STARTED",
        "DATASET_PROFILE_COMPLETED",
        "MISSING_DATA_ANALYSIS_COMPLETED",
        "DUPLICATE_ANALYSIS_COMPLETED",
        "TYPE_FORMAT_ANALYSIS_COMPLETED",
        "OUTLIER_ANALYSIS_COMPLETED",
        "CONSISTENCY_ANALYSIS_COMPLETED",
        "FINDINGS_PRIORITIZED",
        "FINAL_REPORT_GENERATED",
        "MISSION_COMPLETED",
    ]:
        assert expected in event_types

    timestamps = [event["timestamp"] for event in final["events"]]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_parse_failure_marks_mission_failed(client: AsyncClient) -> None:
    upload = await client.post(
        "/datasets",
        files={"file": ("invalid_header.csv", INVALID_CSV.read_bytes(), "text/csv")},
    )
    assert upload.status_code == 201
    created = await client.post(
        "/missions",
        json={"goal": GOAL, "dataset_id": upload.json()["dataset_id"]},
    )
    final = await wait_for_mission_status(
        client, created.json()["mission_id"], {"COMPLETED", "FAILED"}
    )
    assert final["status"] == "FAILED"
    assert final["error"]
    event_types = [event["type"] for event in final["events"]]
    assert "MISSION_FAILED" in event_types
    assert final["investigation_report"] is None
