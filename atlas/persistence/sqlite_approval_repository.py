"""SQLite approval persistence."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from atlas.domain.enums import ApprovalStatus
from atlas.domain.models import ApprovalRequest, utc_now
from atlas.persistence.approval_base import ApprovalRepository
from atlas.persistence.codec import approval_to_document, document_to_approval


class SQLiteApprovalRepository(ApprovalRepository):
    """Stores approval requests as JSON rows in the mission database file."""

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
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_fingerprints (
                    fingerprint TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL,
                    mission_id TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_mission ON approvals(mission_id)"
            )
            await db.commit()
        self._initialized = True

    async def upsert(self, record: ApprovalRequest) -> ApprovalRequest:
        await self._ensure_initialized()
        existing = await self.find_by_fingerprint(record.fingerprint)
        if existing is not None and existing.mission_id == record.mission_id:
            if (
                existing.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                and record.status == ApprovalStatus.PENDING
                and existing.approval_id != record.approval_id
            ):
                return existing
            if existing.status == record.status and existing.approval_id == record.approval_id:
                pass
            elif record.status == ApprovalStatus.PENDING and existing.status not in {
                ApprovalStatus.PENDING,
                ApprovalStatus.APPROVED,
            }:
                record.requested_at = utc_now()
            elif existing.approval_id != record.approval_id and record.status != ApprovalStatus.PENDING:
                record.approval_id = existing.approval_id
                record.requested_at = existing.requested_at
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO approvals (
                    approval_id, mission_id, fingerprint, status, data,
                    requested_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status=excluded.status,
                    data=excluded.data,
                    expires_at=excluded.expires_at
                """,
                (
                    record.approval_id,
                    record.mission_id,
                    record.fingerprint,
                    record.status.value,
                    json.dumps(approval_to_document(record)),
                    record.requested_at.isoformat(),
                    record.expires_at.isoformat(),
                ),
            )
            await db.execute(
                """
                INSERT INTO approval_fingerprints (fingerprint, approval_id, mission_id)
                VALUES (?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    approval_id=excluded.approval_id,
                    mission_id=excluded.mission_id
                """,
                (record.fingerprint, record.approval_id, record.mission_id),
            )
            await db.commit()
        return record

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return document_to_approval(json.loads(row[0]))

    async def find_by_fingerprint(self, fingerprint: str) -> ApprovalRequest | None:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT approval_id FROM approval_fingerprints WHERE fingerprint = ?",
                (fingerprint,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return await self.get(row[0])

    async def list_for_mission(self, mission_id: str) -> list[ApprovalRequest]:
        await self._ensure_initialized()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                """
                SELECT data FROM approvals
                WHERE mission_id = ?
                ORDER BY requested_at DESC
                """,
                (mission_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        records = [document_to_approval(json.loads(row[0])) for row in rows]
        records.sort(key=lambda item: item.requested_at, reverse=True)
        return records

    async def list_expired_pending(self) -> list[ApprovalRequest]:
        await self._ensure_initialized()
        now = utc_now()
        async with aiosqlite.connect(self._database_path) as db:
            async with db.execute(
                "SELECT data FROM approvals WHERE status = ?",
                (ApprovalStatus.PENDING.value,),
            ) as cursor:
                rows = await cursor.fetchall()
        records = [document_to_approval(json.loads(row[0])) for row in rows]
        return [item for item in records if item.expires_at <= now]
