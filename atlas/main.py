"""ATLAS FastAPI application entry point (Cloud Run API / local server)."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from atlas import __version__
from atlas.api.routes import approvals, datasets, health, learning, memory, missions, ops, pubsub
from atlas.config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler. Logs backends, never secrets."""
    settings = get_settings()
    diagnostics = settings.public_diagnostics()
    logger.info(
        "Starting ATLAS API v%s | environment=%s | %s",
        __version__,
        settings.environment.value,
        diagnostics,
    )
    yield
    logger.info("Shutting down ATLAS API")


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
    app.include_router(approvals.router)
    app.include_router(memory.router)
    app.include_router(learning.router)
    app.include_router(ops.router)
    app.include_router(pubsub.router)
    return app


app = create_app()
