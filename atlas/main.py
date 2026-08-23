"""ATLAS FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from atlas import __version__
from atlas.api.routes import datasets, health, missions
from atlas.config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    settings = get_settings()
    backend = settings.resolved_planner_backend.value
    logger.info(
        "Starting ATLAS v%s | environment=%s | planner=%s | adk_configured=%s",
        __version__,
        settings.environment.value,
        backend,
        settings.adk_configured,
    )
    yield
    logger.info("Shutting down ATLAS")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="Autonomous operations agent for the Google All Things Agentic Hackathon 2026",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(datasets.router)
    app.include_router(missions.router)
    return app


app = create_app()
