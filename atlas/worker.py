"""Cloud Run worker entrypoint.

Same application codebase as the API. This process receives Pub/Sub push
messages and executes durable missions. It does not run a local asyncio
background dispatcher.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from atlas import __version__
from atlas.api.routes import health, pubsub
from atlas.config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    diagnostics = settings.public_diagnostics()
    logger.info(
        "Starting ATLAS worker v%s | %s",
        __version__,
        diagnostics,
    )
    yield
    logger.info("Shutting down ATLAS worker")


def create_worker_app() -> FastAPI:
    """HTTP app for Pub/Sub push on Cloud Run."""
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} Worker",
        description="ATLAS mission worker. Accepts Pub/Sub push and executes durable missions.",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(pubsub.router)
    return app


app = create_worker_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "atlas.worker:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
