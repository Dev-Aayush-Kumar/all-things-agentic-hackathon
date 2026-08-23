"""Shared test fixtures."""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Force local fallback and fast execution before app import
os.environ.setdefault("PLANNER_BACKEND", "local")
os.environ.setdefault("ATLAS_STEP_DELAY_SECONDS", "0")

from atlas.api.dependencies import get_app_settings, get_mission_service
from atlas.main import create_app


@pytest.fixture(autouse=True)
def test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Configure isolated test environment for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_atlas.db"
        monkeypatch.setenv("PLANNER_BACKEND", "local")
        monkeypatch.setenv("ATLAS_STEP_DELAY_SECONDS", "0")
        monkeypatch.setenv("ATLAS_DATABASE_PATH", str(db_path))
        get_app_settings.cache_clear()
        get_mission_service.cache_clear()
        yield db_path
        get_app_settings.cache_clear()
        get_mission_service.cache_clear()


@pytest.fixture
def app():
    """Create a fresh FastAPI app instance."""
    get_app_settings.cache_clear()
    get_mission_service.cache_clear()
    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """Async HTTP client for API tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def wait_for_mission_status(
    client: AsyncClient,
    mission_id: str,
    target_statuses: set[str],
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> dict:
    """Poll mission endpoint until target status is reached."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_payload: dict = {}

    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/missions/{mission_id}")
        assert response.status_code == 200
        last_payload = response.json()
        if last_payload["status"] in target_statuses:
            return last_payload
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Mission {mission_id} did not reach {target_statuses}. "
        f"Last status: {last_payload.get('status')}"
    )
