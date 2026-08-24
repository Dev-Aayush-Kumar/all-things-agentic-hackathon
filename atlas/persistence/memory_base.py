"""Memory repository interface."""

from typing import Protocol

from atlas.domain.models import MemoryRecord


class MemoryRepository(Protocol):
    """Abstract persistence for durable memories."""

    async def upsert(self, record: MemoryRecord) -> MemoryRecord:
        """Insert or merge by fingerprint."""
        ...

    async def get(self, memory_id: str) -> MemoryRecord | None:
        ...

    async def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        ...

    async def list_candidates(self, *, limit: int = 200) -> list[MemoryRecord]:
        ...

    async def list_public(
        self,
        *,
        memory_type: str | None = None,
        scope: str | None = None,
        dataset_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        ...
