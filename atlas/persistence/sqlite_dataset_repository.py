"""SQLite dataset metadata repository."""

import json
from pathlib import Path

import aiosqlite

from atlas.domain.models import DatasetRecord
from atlas.persistence.dataset_base import DatasetRepository


class SQLiteDatasetRepository(DatasetRepository):
    """SQLite-backed dataset metadata storage."""

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
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def create(self, record: DatasetRecord) -> DatasetRecord:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO datasets (dataset_id, data, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    record.dataset_id,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
            await db.commit()
        return record

    async def get(self, dataset_id: str) -> DatasetRecord | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return DatasetRecord.model_validate(json.loads(row[0]))
