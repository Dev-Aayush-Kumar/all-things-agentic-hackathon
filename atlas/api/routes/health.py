"""Health check routes."""

from fastapi import APIRouter

from atlas import __version__
from atlas.api.dependencies import get_app_settings
from atlas.domain.models import HealthResponse
from atlas.runtime.tls import native_tls_configured

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Return service health status. Does not fail when local fallback is active."""
    settings = get_app_settings()
    return HealthResponse(
        status="healthy",
        app=settings.app_name,
        version=__version__,
        runtime_mode=settings.resolved_runtime_mode.value,
        runtime_role=settings.resolved_runtime_role.value,
        planner_backend=settings.resolved_planner_backend.value,
        planner_label=settings.planner_label,
        gemini_model=settings.gemini_model,
        gemini_transport=settings.gemini_transport.value,
        gemini_meets_minimum=settings.gemini_meets_minimum,
        persistence_backend=settings.resolved_persistence.value,
        storage_backend=settings.resolved_storage.value,
        dispatcher_backend=settings.resolved_dispatcher.value,
        adk_configured=settings.adk_configured,
        native_tls=native_tls_configured(),
    )


@router.get("/ready")
async def readiness() -> dict:
    """Configuration readiness. Does not call Google Cloud or Gemini."""
    settings = get_app_settings()
    issues: list[str] = []
    if settings.resolved_persistence.value == "firestore" and not settings.firestore_configured:
        issues.append("firestore requires GOOGLE_CLOUD_PROJECT")
    if settings.resolved_storage.value == "gcs" and not settings.gcs_configured:
        issues.append("gcs requires ATLAS_GCS_BUCKET")
    if settings.resolved_dispatcher.value == "pubsub" and not settings.pubsub_configured:
        issues.append("pubsub requires GOOGLE_CLOUD_PROJECT and ATLAS_PUBSUB_TOPIC")
    return {
        "status": "ready" if not issues else "not_ready",
        "runtime_mode": settings.resolved_runtime_mode.value,
        "issues": issues,
    }
