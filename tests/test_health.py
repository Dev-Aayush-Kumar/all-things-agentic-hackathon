"""Health endpoint tests."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_healthy(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["app"] == "ATLAS"
    assert "version" in payload
    assert payload["planner_backend"] == "local_fallback"
    assert payload["adk_configured"] is False
