"""SQLite mission repository for local development."""

import json
from pathlib import Path

import aiosqlite

from atlas.domain.models import Mission
from atlas.persistence.base import MissionRepository


class SQLiteMissionRepository(MissionRepository):
    """SQLite-backed mission storage."""

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
                CREATE TABLE IF NOT EXISTS missions (
                    mission_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def create(self, mission: Mission) -> Mission:
        await self._ensure_initialized()
        payload = mission.model_dump_json()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO missions (mission_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    mission.mission_id,
                    payload,
                    mission.created_at.isoformat(),
                    mission.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return mission

    async def get(self, mission_id: str) -> Mission | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM missions WHERE mission_id = ?",
                (mission_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return Mission.model_validate(json.loads(row[0]))

    async def update(self, mission: Mission) -> Mission:
        await self._ensure_initialized()
        payload = mission.model_dump_json()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE missions
                SET data = ?, updated_at = ?
                WHERE mission_id = ?
                """,
                (
                    payload,
                    mission.updated_at.isoformat(),
                    mission.mission_id,
                ),
            )
            await db.commit()
        return mission

    async def delete(self, mission_id: str) -> None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                "DELETE FROM missions WHERE mission_id = ?",
                (mission_id,),
            )
            await db.commit()
