"""Optional live Google Cloud checks.

Skipped unless ATLAS_LIVE_CLOUD=1. The default test suite never calls Google APIs.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.live_cloud,
    pytest.mark.skipif(
        os.environ.get("ATLAS_LIVE_CLOUD") != "1",
        reason="Live Google Cloud tests are opt-in via ATLAS_LIVE_CLOUD=1",
    ),
]


def test_application_default_credentials_available() -> None:
    try:
        import google.auth
    except ImportError as exc:
        pytest.skip(f"google-auth is not installed: {exc}")
    credentials, project = google.auth.default()
    assert credentials is not None
    assert project or os.environ.get("GOOGLE_CLOUD_PROJECT")


@pytest.mark.asyncio
async def test_gemini_model_is_reachable_if_configured() -> None:
    from atlas.agent.gemini import DEFAULT_GEMINI_MODEL
    from atlas.config.settings import Settings

    settings = Settings()
    if not settings.adk_configured:
        pytest.skip("No Gemini credentials in this environment")
    from google import genai

    client = genai.Client()
    models = client.models.list()
    names = [getattr(model, "name", "") for model in models]
    expected = settings.gemini_model or DEFAULT_GEMINI_MODEL
    assert any(expected in name for name in names), (
        f"Configured GEMINI_MODEL={expected} was not present in the live model list"
    )
