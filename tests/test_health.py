"""Health endpoint tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from atlas.api.dependencies import (
    get_app_settings,
    get_approval_service,
    get_dataset_service,
    get_mission_service,
    get_mission_worker,
)
from atlas.config.settings import get_settings
from atlas.main import create_app


@pytest.mark.asyncio
async def test_health_returns_healthy(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["app"] == "ATLAS"
    assert "version" in payload
    assert payload["runtime_mode"] == "local"
    assert payload["runtime_role"] == "api"
    assert payload["planner_backend"] == "local_fallback"
    assert payload["planner_label"] == "LOCAL_DEVELOPMENT_FALLBACK"
    assert payload["gemini_model"] == "gemini-3.5-flash"
    assert payload["gemini_transport"] == "none"
    assert payload["persistence_backend"] == "sqlite"
    assert payload["storage_backend"] == "local_fs"
    assert payload["dispatcher_backend"] == "local_async"
    assert payload["adk_configured"] is False
    assert payload["native_tls"] is True
    assert "google_api_key" not in payload
    assert "GOOGLE_API_KEY" not in response.text


@pytest.mark.asyncio
async def test_ready_is_ready_in_local_mode(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["runtime_mode"] == "local"
    assert payload["issues"] == []


@pytest.mark.asyncio
async def test_health_does_not_fail_when_fallback_is_active(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["planner_label"] == "LOCAL_DEVELOPMENT_FALLBACK"


@pytest.mark.asyncio
async def test_health_and_ready_do_not_expose_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret-key-value")
    monkeypatch.setenv("PLANNER_BACKEND", "local")
    monkeypatch.setenv("ATLAS_RUNTIME_MODE", "local")
    get_settings.cache_clear()
    get_app_settings.cache_clear()
    get_mission_service.cache_clear()
    get_dataset_service.cache_clear()
    get_mission_worker.cache_clear()
    get_approval_service.cache_clear()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200
    assert ready.status_code == 200
    combined = health.text + ready.text + str(health.json()) + str(ready.json())
    assert "super-secret-key-value" not in combined
    assert "google_api_key" not in health.json()


@pytest.mark.asyncio
async def test_ready_reports_missing_cloud_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAS_RUNTIME_MODE", "cloud")
    monkeypatch.setenv("ATLAS_PERSISTENCE", "auto")
    monkeypatch.setenv("ATLAS_STORAGE", "auto")
    monkeypatch.setenv("ATLAS_DISPATCHER", "auto")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setenv("ATLAS_GCS_BUCKET", "")
    monkeypatch.setenv("ATLAS_PUBSUB_TOPIC", "")
    get_settings.cache_clear()
    get_app_settings.cache_clear()
    get_mission_service.cache_clear()
    get_dataset_service.cache_clear()
    get_mission_worker.cache_clear()
    get_approval_service.cache_clear()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        ready = await client.get("/ready")
    assert health.status_code == 200
    assert health.json()["runtime_mode"] == "cloud"
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "not_ready"
    assert any("firestore" in issue for issue in payload["issues"])
    assert any("gcs" in issue for issue in payload["issues"])
    assert any("pubsub" in issue for issue in payload["issues"])
