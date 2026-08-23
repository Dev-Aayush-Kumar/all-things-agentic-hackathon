"""Background executor interface."""

from collections.abc import Awaitable, Callable
from typing import Protocol


class BackgroundExecutor(Protocol):
    """Abstract interface for dispatching background work."""

    def submit(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        """Schedule a coroutine to run in the background."""
        ...
