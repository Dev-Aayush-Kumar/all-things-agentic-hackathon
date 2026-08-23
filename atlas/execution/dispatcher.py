"""Mission dispatch abstraction.

Local development uses in-process asyncio. Production can swap in a
Pub/Sub (or equivalent) dispatcher later. The Pub/Sub class in this
package is an explicit stub, not a working cloud deployment.
"""

from __future__ import annotations

from typing import Protocol

from atlas.domain.exceptions import CloudDispatchNotConfiguredError
from atlas.execution.base import BackgroundExecutor
from atlas.execution.worker import MissionWorker


class MissionDispatcher(Protocol):
    """Sends a persisted mission to a worker."""

    @property
    def backend_name(self) -> str:
        """Stable name of this dispatcher implementation."""
        ...

    async def dispatch(self, mission_id: str) -> None:
        """Request execution of a queued mission."""
        ...


class LocalAsyncDispatcher:
    """Development dispatcher: schedule a worker coroutine in this process."""

    def __init__(self, executor: BackgroundExecutor, worker: MissionWorker) -> None:
        self._executor = executor
        self._worker = worker

    @property
    def backend_name(self) -> str:
        return "local_async"

    async def dispatch(self, mission_id: str) -> None:
        self._executor.submit(lambda: self._worker.execute(mission_id))


class PubSubDispatcherStub:
    """Placeholder for a future Pub/Sub dispatcher.

    This is not a configured or deployed Pub/Sub integration.
    """

    @property
    def backend_name(self) -> str:
        return "pubsub_stub"

    @property
    def configured(self) -> bool:
        return False

    async def dispatch(self, mission_id: str) -> None:
        raise CloudDispatchNotConfiguredError(
            "Pub/Sub dispatch is not implemented in this round. "
            "ATLAS_DISPATCHER=local is required for local development. "
            f"Refusing to pretend mission '{mission_id}' was published."
        )
