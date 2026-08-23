"""Dataset binary storage interface."""

from typing import Protocol


class DatasetStorage(Protocol):
    """Stores uploaded dataset bytes. Replaceable with Cloud Storage later."""

    async def save(self, stored_filename: str, content: bytes) -> str:
        """Persist file bytes and return the storage key."""
        ...

    async def load(self, stored_filename: str) -> bytes:
        """Load file bytes by storage key."""
        ...
