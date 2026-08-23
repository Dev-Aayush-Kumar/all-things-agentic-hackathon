"""Mission dispatch abstraction.

LOCAL: in-process asyncio worker.
CLOUD: Google Cloud Pub/Sub publisher.
"""

from __future__ import annotations

from typing import Protocol

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
