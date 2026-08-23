"""Background executor and dispatcher factories."""

import logging

from atlas.config.settings import DispatcherBackend, Settings
from atlas.domain.exceptions import CloudDispatchNotConfiguredError
from atlas.execution.base import BackgroundExecutor
from atlas.execution.dispatcher import LocalAsyncDispatcher, MissionDispatcher
from atlas.execution.local_executor import LocalBackgroundExecutor
from atlas.execution.pubsub_dispatcher import PubSubDispatcher
from atlas.execution.worker import MissionWorker

logger = logging.getLogger(__name__)


def create_background_executor() -> BackgroundExecutor:
    """Create the local background executor."""
    return LocalBackgroundExecutor()


def create_dispatcher(
    settings: Settings,
    worker: MissionWorker | None = None,
) -> MissionDispatcher:
    """Create the configured dispatcher. Default local asyncio; Pub/Sub when selected."""
    backend = settings.resolved_dispatcher
    if backend == DispatcherBackend.PUBSUB:
        if not settings.pubsub_configured:
            raise CloudDispatchNotConfiguredError(
                "ATLAS_DISPATCHER=pubsub requires GOOGLE_CLOUD_PROJECT and ATLAS_PUBSUB_TOPIC"
            )
        assert settings.google_cloud_project is not None
        assert settings.pubsub_topic is not None
        logger.info(
            "Using Pub/Sub dispatcher (topic=%s)",
            settings.pubsub_topic,
        )
        return PubSubDispatcher(
            project=settings.google_cloud_project,
            topic=settings.pubsub_topic,
        )
    if worker is None:
        raise RuntimeError("Local dispatcher requires a MissionWorker")
    logger.info("Using local asyncio dispatcher")
    return LocalAsyncDispatcher(create_background_executor(), worker)
