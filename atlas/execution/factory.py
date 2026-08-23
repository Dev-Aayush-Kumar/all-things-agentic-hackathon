"""Background executor factory."""

from atlas.execution.base import BackgroundExecutor
from atlas.execution.local_executor import LocalBackgroundExecutor


def create_background_executor() -> BackgroundExecutor:
    """Create the local background executor."""
    return LocalBackgroundExecutor()
