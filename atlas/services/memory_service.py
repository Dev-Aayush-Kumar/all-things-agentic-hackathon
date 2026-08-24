"""Read-only memory inspection."""

from atlas.domain.models import MemoryListResponse, MemoryRecordPublic
from atlas.persistence.memory_base import MemoryRepository


def public_memory(record) -> MemoryRecordPublic:
    return MemoryRecordPublic(
        memory_id=record.memory_id,
        type=record.type,
        content=record.content,
        scope=record.scope,
        scope_ref=record.scope_ref,
        tags=list(record.tags),
        confidence=record.confidence,
        provenance=list(record.provenance),
        extraction_source=record.extraction_source,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class MemoryService:
    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    async def get(self, memory_id: str) -> MemoryRecordPublic | None:
        record = await self._repository.get(memory_id)
        if record is None:
            return None
        return public_memory(record)

    async def list(
        self,
        *,
        memory_type: str | None = None,
        scope: str | None = None,
        dataset_id: str | None = None,
        limit: int = 50,
    ) -> MemoryListResponse:
        records = await self._repository.list_public(
            memory_type=memory_type,
            scope=scope,
            dataset_id=dataset_id,
            limit=min(limit, 100),
        )
        items = [public_memory(item) for item in records]
        return MemoryListResponse(items=items, count=len(items))
