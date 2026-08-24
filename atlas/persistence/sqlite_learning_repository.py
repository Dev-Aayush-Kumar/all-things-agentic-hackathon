"""SQLite experience and strategy persistence."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from atlas.domain.models import ExperienceRecord, StrategyRecord
from atlas.persistence.learning_base import ExperienceRepository, StrategyRepository


class SQLiteLearningRepository(ExperienceRepository, StrategyRepository):
    """Stores experiences and strategies as JSON rows in the mission database file."""

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
                CREATE TABLE IF NOT EXISTS experiences (
                    experience_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    mission_id TEXT NOT NULL UNIQUE,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    strategy_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    mission_category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def upsert(self, record):
        if isinstance(record, ExperienceRecord):
            return await self._upsert_experience(record)
        return await self._upsert_strategy(record)

    async def _upsert_experience(self, record: ExperienceRecord) -> ExperienceRecord:
        await self._ensure_initialized()
        existing = await self.find_by_fingerprint(record.fingerprint)
        stored = record
        if existing is not None:
            stored.experience_id = existing.experience_id
            stored.created_at = existing.created_at
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO experiences (
                    experience_id, fingerprint, mission_id, data, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    data=excluded.data
                """,
                (
                    stored.experience_id,
                    stored.fingerprint,
                    stored.mission_id,
                    stored.model_dump_json(),
                    stored.created_at.isoformat(),
                ),
            )
            await db.commit()
        return stored

    async def _upsert_strategy(self, record: StrategyRecord) -> StrategyRecord:
        await self._ensure_initialized()
        existing = await self.find_by_fingerprint(record.fingerprint)
        stored = record
        if existing is not None:
            stored.strategy_id = existing.strategy_id
            stored.created_at = existing.created_at
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO strategies (
                    strategy_id, fingerprint, mission_category, confidence,
                    data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    data=excluded.data,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
                """,
                (
                    stored.strategy_id,
                    stored.fingerprint,
                    stored.mission_category.value,
                    stored.confidence,
                    stored.model_dump_json(),
                    stored.created_at.isoformat(),
                    stored.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return stored

    async def get(self, record_id: str):
        experience = await self._get_experience(record_id)
        if experience is not None:
            return experience
        return await self._get_strategy(record_id)

    async def _get_experience(self, experience_id: str) -> ExperienceRecord | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM experiences WHERE experience_id = ?",
                (experience_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return ExperienceRecord.model_validate(json.loads(row[0]))

    async def _get_strategy(self, strategy_id: str) -> StrategyRecord | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM strategies WHERE strategy_id = ?",
                (strategy_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return StrategyRecord.model_validate(json.loads(row[0]))

    async def get_by_mission(self, mission_id: str) -> ExperienceRecord | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM experiences WHERE mission_id = ?",
                (mission_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return ExperienceRecord.model_validate(json.loads(row[0]))

    async def find_by_fingerprint(self, fingerprint: str):
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM experiences WHERE fingerprint = ?",
                (fingerprint,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is not None:
            return ExperienceRecord.model_validate(json.loads(row[0]))
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM strategies WHERE fingerprint = ?",
                (fingerprint,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return StrategyRecord.model_validate(json.loads(row[0]))

    async def list_candidates(self, *, limit: int = 100) -> list[StrategyRecord]:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM strategies ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [StrategyRecord.model_validate(json.loads(row[0])) for row in rows]

    async def list_public(self, *, limit: int = 50) -> list[StrategyRecord]:
        return await self.list_candidates(limit=limit)
