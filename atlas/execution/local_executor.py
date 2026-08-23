"""Local asyncio background executor."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from atlas.execution.base import BackgroundExecutor

logger = logging.getLogger(__name__)


class LocalBackgroundExecutor(BackgroundExecutor):
    """Runs background coroutines using asyncio tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._run(coro_factory))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        try:
            await coro_factory()
        except Exception:
            logger.exception("Background task failed")
