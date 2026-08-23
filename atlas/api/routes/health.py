"""Health check routes."""

from fastapi import APIRouter

from atlas import __version__
from atlas.api.dependencies import get_app_settings
from atlas.domain.models import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Return service health status."""
    settings = get_app_settings()
    backend = settings.resolved_planner_backend.value
    return HealthResponse(
        status="healthy",
        app=settings.app_name,
        version=__version__,
        planner_backend=backend,
        adk_configured=settings.adk_configured,
    )
