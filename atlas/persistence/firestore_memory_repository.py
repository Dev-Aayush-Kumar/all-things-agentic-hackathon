"""Firestore (or in-memory document store) memory persistence."""

from __future__ import annotations

from typing import Any

from atlas.domain.models import MemoryRecord
from atlas.ops.memory.policy import merge_records
from atlas.persistence.codec import document_to_memory, memory_to_document
from atlas.persistence.memory_base import MemoryRepository

MEMORIES = "memories"
FINGERPRINTS = "memory_fingerprints"


class FirestoreMemoryRepository(MemoryRepository):
    """Uses the same document-store API as mission Firestore persistence."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def upsert(self, record: MemoryRecord) -> MemoryRecord:
        existing_id = await self._store.get_document(FINGERPRINTS, record.fingerprint)
        if existing_id and existing_id.get("memory_id"):
            current = await self.get(str(existing_id["memory_id"]))
            if current is not None:
                record = merge_records(current, record)
                record.memory_id = current.memory_id
                record.created_at = current.created_at
        await self._store.set_document(MEMORIES, record.memory_id, memory_to_document(record))
        await self._store.set_document(
            FINGERPRINTS,
            record.fingerprint,
            {"memory_id": record.memory_id, "fingerprint": record.fingerprint},
        )
        return record

    async def get(self, memory_id: str) -> MemoryRecord | None:
        document = await self._store.get_document(MEMORIES, memory_id)
        if document is None:
            return None
        return document_to_memory(document)

    async def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        pointer = await self._store.get_document(FINGERPRINTS, fingerprint)
        if pointer is None or not pointer.get("memory_id"):
            return None
        return await self.get(str(pointer["memory_id"]))

    async def list_candidates(self, *, limit: int = 200) -> list[MemoryRecord]:
        rows = await self._store.list_documents(MEMORIES)
        records = [document_to_memory(data) for _, data in rows]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit]

    async def list_public(
        self,
        *,
        memory_type: str | None = None,
        scope: str | None = None,
        dataset_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        filtered: list[MemoryRecord] = []
        for item in await self.list_candidates(limit=max(limit, 200)):
            if memory_type and item.type.value != memory_type:
                continue
            if scope and item.scope.value != scope:
                continue
            if dataset_id and not (
                item.scope_ref == dataset_id or item.scope.value == "GLOBAL"
            ):
                continue
            filtered.append(item)
            if len(filtered) >= limit:
                break
        return filtered
