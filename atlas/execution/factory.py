"""Background executor and dispatcher factories."""

from atlas.config.settings import Settings
from atlas.execution.base import BackgroundExecutor
from atlas.execution.dispatcher import (
    LocalAsyncDispatcher,
    MissionDispatcher,
    PubSubDispatcherStub,
)
from atlas.execution.local_executor import LocalBackgroundExecutor
from atlas.execution.worker import MissionWorker


def create_background_executor() -> BackgroundExecutor:
    """Create the local background executor."""
    return LocalBackgroundExecutor()


def create_dispatcher(settings: Settings, worker: MissionWorker) -> MissionDispatcher:
    """Create the configured dispatcher. Default is local asyncio."""
    backend = settings.dispatcher.strip().lower()
    if backend in {"pubsub", "pub_sub", "cloud"}:
        return PubSubDispatcherStub()
    return LocalAsyncDispatcher(create_background_executor(), worker)
