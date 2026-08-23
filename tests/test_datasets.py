"""Dataset upload API tests."""

import pytest
from httpx import AsyncClient

from tests.conftest import FIXTURES_DIR

SURVEY_CSV = FIXTURES_DIR / "survey_quality.csv"


@pytest.mark.asyncio
async def test_csv_upload_succeeds(client: AsyncClient) -> None:
    content = SURVEY_CSV.read_bytes()
    response = await client.post(
        "/datasets",
        files={"file": ("survey_quality.csv", content, "text/csv")},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["original_filename"] == "survey_quality.csv"
    assert payload["stored_filename"].endswith(".csv")
    assert payload["stored_filename"] != "survey_quality.csv"
    assert payload["size_bytes"] == len(content)
    assert "dataset_id" in payload
    assert "created_at" in payload
    assert "/" not in payload["stored_filename"]
    assert "\\" not in payload["stored_filename"]


@pytest.mark.asyncio
async def test_unsupported_file_type_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/datasets",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 422
    assert "csv" in response.json()["detail"].lower()

    response = await client.post(
        "/datasets",
        files={"file": ("workbook.xlsx", b"not-excel", "application/vnd.ms-excel")},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_empty_upload_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/datasets",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_dataset_metadata_can_be_retrieved(client: AsyncClient) -> None:
    upload = await client.post(
        "/datasets",
        files={"file": ("survey_quality.csv", SURVEY_CSV.read_bytes(), "text/csv")},
    )
    dataset_id = upload.json()["dataset_id"]

    response = await client.get(f"/datasets/{dataset_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["dataset_id"] == dataset_id
    assert payload["original_filename"] == "survey_quality.csv"
    assert payload["size_bytes"] == SURVEY_CSV.stat().st_size


@pytest.mark.asyncio
async def test_unknown_dataset_returns_404(client: AsyncClient) -> None:
    response = await client.get("/datasets/not-a-real-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_path_traversal_filename_is_sanitized(client: AsyncClient) -> None:
    response = await client.post(
        "/datasets",
        files={"file": ("../../secret.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["original_filename"] == "secret.csv"
    assert ".." not in payload["stored_filename"]
