"""SQLite memory persistence."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from atlas.domain.models import MemoryRecord
from atlas.ops.memory.policy import merge_records
from atlas.persistence.memory_base import MemoryRepository


class SQLiteMemoryRepository(MemoryRepository):
    """Stores memories as JSON rows keyed by memory_id and fingerprint."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    memory_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_ref TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def upsert(self, record: MemoryRecord) -> MemoryRecord:
        await self._ensure_initialized()
        existing = await self.find_by_fingerprint(record.fingerprint)
        stored = merge_records(existing, record) if existing is not None else record
        if existing is not None:
            stored.memory_id = existing.memory_id
            stored.created_at = existing.created_at
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO memories (
                    memory_id, fingerprint, memory_type, scope, scope_ref,
                    data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    stored.memory_id,
                    stored.fingerprint,
                    stored.type.value,
                    stored.scope.value,
                    stored.scope_ref,
                    stored.model_dump_json(),
                    stored.created_at.isoformat(),
                    stored.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return stored

    async def get(self, memory_id: str) -> MemoryRecord | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM memories WHERE memory_id = ?",
                (memory_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return MemoryRecord.model_validate(json.loads(row[0]))

    async def find_by_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM memories WHERE fingerprint = ?",
                (fingerprint,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return MemoryRecord.model_validate(json.loads(row[0]))

    async def list_candidates(self, *, limit: int = 200) -> list[MemoryRecord]:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM memories ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [MemoryRecord.model_validate(json.loads(row[0])) for row in rows]

    async def list_public(
        self,
        *,
        memory_type: str | None = None,
        scope: str | None = None,
        dataset_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        records = await self.list_candidates(limit=max(limit, 200))
        filtered: list[MemoryRecord] = []
        for item in records:
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
