"""SQLite mission repository for local development."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite

from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.exceptions import IdempotencyConflictError, StaleExecutionError
from atlas.domain.models import Mission, MissionEvent, utc_now
from atlas.execution.context import ExecutionContext
from atlas.persistence.base import MissionRepository

TERMINAL_LIFECYCLE = (MissionStatus.COMPLETED.value, MissionStatus.FAILED.value)
TERMINAL_EXECUTION = (
    ExecutionState.COMPLETED.value,
    ExecutionState.FAILED.value,
    ExecutionState.EXHAUSTED.value,
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


class SQLiteMissionRepository(MissionRepository):
    """SQLite-backed mission storage with atomic claim/lease columns."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._initialized = False

    async def _connect(self) -> aiosqlite.Connection:
        await self._ensure_initialized()
        db = await aiosqlite.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 30000")
        return db

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path, timeout=30.0) as db:
            await db.execute("PRAGMA busy_timeout = 30000")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    mission_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    execution_state TEXT NOT NULL DEFAULT 'QUEUED',
                    execution_id TEXT,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_fingerprint TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._migrate_columns(db)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_missions_execution_state "
                "ON missions(execution_state)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_missions_lease "
                "ON missions(lease_expires_at)"
            )
            await db.commit()
        self._initialized = True

    async def _migrate_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(missions)")
        columns = {row[1] for row in await cursor.fetchall()}
        additions = {
            "status": "ALTER TABLE missions ADD COLUMN status TEXT NOT NULL DEFAULT 'CREATED'",
            "execution_state": (
                "ALTER TABLE missions ADD COLUMN execution_state TEXT NOT NULL DEFAULT 'QUEUED'"
            ),
            "execution_id": "ALTER TABLE missions ADD COLUMN execution_id TEXT",
            "worker_id": "ALTER TABLE missions ADD COLUMN worker_id TEXT",
            "lease_expires_at": "ALTER TABLE missions ADD COLUMN lease_expires_at TEXT",
            "attempt_count": (
                "ALTER TABLE missions ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
            ),
            "max_attempts": (
                "ALTER TABLE missions ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3"
            ),
        }
        for name, statement in additions.items():
            if name not in columns:
                await db.execute(statement)

    async def create(self, mission: Mission) -> Mission:
        await self._ensure_initialized()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._insert_mission(db, mission)
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()
        return mission

    async def create_idempotent(
        self,
        mission: Mission,
        idempotency_key: str,
        payload_fingerprint: str,
    ) -> tuple[Mission, bool]:
        await self._ensure_initialized()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            existing = await self._get_idempotency(db, idempotency_key)
            if existing is not None:
                stored_fingerprint, stored_mission_id = existing
                if stored_fingerprint != payload_fingerprint:
                    await db.execute("ROLLBACK")
                    raise IdempotencyConflictError(idempotency_key)
                loaded = await self._load_mission(db, stored_mission_id)
                await db.commit()
                if loaded is None:
                    raise RuntimeError(
                        f"Idempotency key '{idempotency_key}' points to missing mission"
                    )
                return loaded, True

            try:
                await db.execute(
                    """
                    INSERT INTO mission_idempotency
                        (idempotency_key, payload_fingerprint, mission_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        payload_fingerprint,
                        mission.mission_id,
                        _iso(mission.created_at),
                    ),
                )
            except aiosqlite.IntegrityError:
                await db.execute("ROLLBACK")
                stored = await self._get_idempotency(db, idempotency_key)
                if stored is None:
                    raise IdempotencyConflictError(idempotency_key)
                stored_fingerprint, stored_mission_id = stored
                if stored_fingerprint != payload_fingerprint:
                    raise IdempotencyConflictError(idempotency_key)
                loaded = await self._load_mission(db, stored_mission_id)
                if loaded is None:
                    raise RuntimeError(
                        f"Idempotency key '{idempotency_key}' points to missing mission"
                    )
                return loaded, True

            await self._insert_mission(db, mission)
            await db.commit()
            return mission, False
        except IdempotencyConflictError:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()

    async def get(self, mission_id: str) -> Mission | None:
        db = await self._connect()
        try:
            return await self._load_mission(db, mission_id)
        finally:
            await db.close()

    async def update(self, mission: Mission) -> Mission:
        """System update. Workers must use update_owned."""
        mission.touch()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._write_mission(db, mission)
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()
        return mission

    async def update_owned(
        self,
        mission: Mission,
        context: ExecutionContext,
    ) -> Mission:
        mission.touch()
        now = utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE missions
                SET data = ?,
                    status = ?,
                    execution_state = ?,
                    attempt_count = ?,
                    max_attempts = ?,
                    lease_expires_at = CASE
                        WHEN ? IN (?, ?, ?, ?) THEN NULL
                        ELSE lease_expires_at
                    END,
                    worker_id = CASE
                        WHEN ? IN (?, ?, ?, ?) THEN NULL
                        ELSE worker_id
                    END,
                    execution_id = CASE
                        WHEN ? IN (?, ?, ?, ?) THEN NULL
                        ELSE execution_id
                    END,
                    updated_at = ?
                WHERE mission_id = ?
                  AND execution_id = ?
                  AND worker_id = ?
                  AND execution_state IN (?, ?)
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > ?
                """,
                (
                    mission.model_dump_json(),
                    mission.status.value,
                    mission.execution.state.value,
                    mission.execution.attempt_count,
                    mission.execution.max_attempts,
                    mission.execution.state.value,
                    ExecutionState.COMPLETED.value,
                    ExecutionState.FAILED.value,
                    ExecutionState.EXHAUSTED.value,
                    ExecutionState.WAITING_FOR_APPROVAL.value,
                    mission.execution.state.value,
                    ExecutionState.COMPLETED.value,
                    ExecutionState.FAILED.value,
                    ExecutionState.EXHAUSTED.value,
                    ExecutionState.WAITING_FOR_APPROVAL.value,
                    mission.execution.state.value,
                    ExecutionState.COMPLETED.value,
                    ExecutionState.FAILED.value,
                    ExecutionState.EXHAUSTED.value,
                    ExecutionState.WAITING_FOR_APPROVAL.value,
                    _iso(mission.updated_at),
                    mission.mission_id,
                    context.execution_id,
                    context.worker_id,
                    ExecutionState.CLAIMED.value,
                    ExecutionState.RUNNING.value,
                    _iso(now),
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                raise StaleExecutionError(mission.mission_id)
            await db.commit()
        except StaleExecutionError:
            raise
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()
        return mission

    async def claim(
        self,
        mission_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> Mission | None:
        current = now or utc_now()
        execution_id = str(uuid4())
        expires = current + timedelta(seconds=lease_seconds)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE missions
                SET execution_id = ?,
                    worker_id = ?,
                    execution_state = ?,
                    lease_expires_at = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE mission_id = ?
                  AND status NOT IN (?, ?, ?)
                  AND execution_state NOT IN (?, ?, ?, ?)
                  AND attempt_count < max_attempts
                  AND (
                    execution_state = ?
                    OR (
                        execution_state IN (?, ?)
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?
                    )
                  )
                """,
                (
                    execution_id,
                    worker_id,
                    ExecutionState.CLAIMED.value,
                    _iso(expires),
                    _iso(current),
                    mission_id,
                    MissionStatus.COMPLETED.value,
                    MissionStatus.FAILED.value,
                    MissionStatus.WAITING_FOR_APPROVAL.value,
                    ExecutionState.COMPLETED.value,
                    ExecutionState.FAILED.value,
                    ExecutionState.EXHAUSTED.value,
                    ExecutionState.WAITING_FOR_APPROVAL.value,
                    ExecutionState.QUEUED.value,
                    ExecutionState.CLAIMED.value,
                    ExecutionState.RUNNING.value,
                    _iso(current),
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return None

            mission = await self._load_mission(db, mission_id)
            if mission is None:
                await db.execute("ROLLBACK")
                return None

            row = await db.execute(
                "SELECT attempt_count FROM missions WHERE mission_id = ?",
                (mission_id,),
            )
            attempt_row = await row.fetchone()
            attempt_count = int(attempt_row["attempt_count"]) if attempt_row else 1
            if mission.execution.resume_without_attempt:
                attempt_count = max(0, attempt_count - 1)
                mission.execution.resume_without_attempt = False
                await db.execute(
                    "UPDATE missions SET attempt_count = ? WHERE mission_id = ?",
                    (attempt_count, mission_id),
                )

            mission.execution.state = ExecutionState.CLAIMED
            mission.execution.execution_id = execution_id
            mission.execution.worker_id = worker_id
            mission.execution.claimed_at = current
            mission.execution.lease_expires_at = expires
            mission.execution.heartbeat_at = current
            mission.execution.attempt_count = attempt_count
            mission.execution.last_error = None
            mission.events.append(
                MissionEvent(
                    type=EventType.MISSION_CLAIMED,
                    message="Mission claimed by worker",
                    metadata={
                        "worker_id": worker_id,
                        "execution_id": execution_id,
                        "attempt_count": attempt_count,
                    },
                )
            )
            mission.touch()
            await self._write_mission(db, mission)
            await db.commit()
            return mission
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()

    async def renew_lease(
        self,
        mission_id: str,
        context: ExecutionContext,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        current = now or utc_now()
        expires = current + timedelta(seconds=lease_seconds)
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE missions
                SET lease_expires_at = ?,
                    updated_at = ?
                WHERE mission_id = ?
                  AND execution_id = ?
                  AND worker_id = ?
                  AND execution_state IN (?, ?)
                """,
                (
                    _iso(expires),
                    _iso(current),
                    mission_id,
                    context.execution_id,
                    context.worker_id,
                    ExecutionState.CLAIMED.value,
                    ExecutionState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return False
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()

    async def list_recoverable(self, now: datetime | None = None) -> list[Mission]:
        current = now or utc_now()
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT data, status, execution_state, execution_id, worker_id,
                       lease_expires_at, attempt_count, max_attempts
                FROM missions
                WHERE status NOT IN (?, ?)
                  AND execution_state NOT IN (?, ?, ?, ?, ?)
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (
                    MissionStatus.COMPLETED.value,
                    MissionStatus.FAILED.value,
                    ExecutionState.COMPLETED.value,
                    ExecutionState.FAILED.value,
                    ExecutionState.EXHAUSTED.value,
                    ExecutionState.QUEUED.value,
                    ExecutionState.WAITING_FOR_APPROVAL.value,
                    _iso(current),
                ),
            )
            rows = await cursor.fetchall()
            return [self._mission_from_row(row) for row in rows]
        finally:
            await db.close()

    async def requeue_expired(
        self, mission_id: str, now: datetime | None = None
    ) -> Mission | None:
        current = now or utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE missions
                SET execution_state = ?,
                    execution_id = NULL,
                    worker_id = NULL,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE mission_id = ?
                  AND status NOT IN (?, ?)
                  AND execution_state IN (?, ?)
                  AND attempt_count < max_attempts
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (
                    ExecutionState.QUEUED.value,
                    None,
                    _iso(current),
                    mission_id,
                    MissionStatus.COMPLETED.value,
                    MissionStatus.FAILED.value,
                    ExecutionState.CLAIMED.value,
                    ExecutionState.RUNNING.value,
                    _iso(current),
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return None
            mission = await self._load_mission(db, mission_id)
            if mission is None:
                await db.execute("ROLLBACK")
                return None
            previous_worker = mission.execution.worker_id
            previous_execution = mission.execution.execution_id
            mission.execution.state = ExecutionState.QUEUED
            mission.execution.execution_id = None
            mission.execution.worker_id = None
            mission.execution.lease_expires_at = None
            mission.execution.heartbeat_at = None
            mission.events.append(
                MissionEvent(
                    type=EventType.LEASE_EXPIRED,
                    message="Execution lease expired; worker is considered lost",
                    metadata={
                        "previous_worker_id": previous_worker,
                        "previous_execution_id": previous_execution,
                    },
                )
            )
            mission.events.append(
                MissionEvent(
                    type=EventType.MISSION_RECOVERED,
                    message="Mission requeued after lease expiry",
                    metadata={"attempt_count": mission.execution.attempt_count},
                )
            )
            mission.events.append(
                MissionEvent(
                    type=EventType.MISSION_QUEUED,
                    message="Mission queued for dispatch",
                    metadata={"recovered": True},
                )
            )
            mission.touch()
            await self._write_mission(db, mission)
            await db.commit()
            return mission
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()

    async def exhaust_expired(
        self, mission_id: str, now: datetime | None = None
    ) -> Mission | None:
        current = now or utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE missions
                SET execution_state = ?,
                    status = ?,
                    execution_id = NULL,
                    worker_id = NULL,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE mission_id = ?
                  AND status NOT IN (?, ?)
                  AND execution_state IN (?, ?)
                  AND attempt_count >= max_attempts
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                """,
                (
                    ExecutionState.EXHAUSTED.value,
                    MissionStatus.FAILED.value,
                    None,
                    _iso(current),
                    mission_id,
                    MissionStatus.COMPLETED.value,
                    MissionStatus.FAILED.value,
                    ExecutionState.CLAIMED.value,
                    ExecutionState.RUNNING.value,
                    _iso(current),
                ),
            )
            if cursor.rowcount != 1:
                await db.execute("ROLLBACK")
                return None
            mission = await self._load_mission(db, mission_id)
            if mission is None:
                await db.execute("ROLLBACK")
                return None
            mission.status = MissionStatus.FAILED
            mission.error = "Maximum execution attempts exceeded"
            mission.completed_at = current
            mission.execution.state = ExecutionState.EXHAUSTED
            mission.execution.execution_id = None
            mission.execution.worker_id = None
            mission.execution.lease_expires_at = None
            mission.execution.last_error = mission.error
            mission.events.append(
                MissionEvent(
                    type=EventType.LEASE_EXPIRED,
                    message="Execution lease expired after the final attempt",
                    metadata={"attempt_count": mission.execution.attempt_count},
                )
            )
            mission.events.append(
                MissionEvent(
                    type=EventType.EXECUTION_EXHAUSTED,
                    message="Mission exhausted maximum execution attempts",
                    metadata={
                        "attempt_count": mission.execution.attempt_count,
                        "max_attempts": mission.execution.max_attempts,
                    },
                )
            )
            mission.events.append(
                MissionEvent(
                    type=EventType.MISSION_FAILED,
                    message="Mission failed",
                    metadata={"error": mission.error},
                )
            )
            mission.touch()
            await self._write_mission(db, mission)
            await db.commit()
            return mission
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()

    async def delete(self, mission_id: str) -> None:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DELETE FROM missions WHERE mission_id = ?", (mission_id,))
            await db.execute(
                "DELETE FROM mission_idempotency WHERE mission_id = ?",
                (mission_id,),
            )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.close()

    async def _insert_mission(self, db: aiosqlite.Connection, mission: Mission) -> None:
        await db.execute(
            """
            INSERT INTO missions (
                mission_id, data, status, execution_state, execution_id, worker_id,
                lease_expires_at, attempt_count, max_attempts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission.mission_id,
                mission.model_dump_json(),
                mission.status.value,
                mission.execution.state.value,
                mission.execution.execution_id,
                mission.execution.worker_id,
                _iso(mission.execution.lease_expires_at),
                mission.execution.attempt_count,
                mission.execution.max_attempts,
                _iso(mission.created_at),
                _iso(mission.updated_at),
            ),
        )

    async def _write_mission(self, db: aiosqlite.Connection, mission: Mission) -> None:
        await db.execute(
            """
            UPDATE missions
            SET data = ?,
                status = ?,
                execution_state = ?,
                execution_id = ?,
                worker_id = ?,
                lease_expires_at = ?,
                attempt_count = ?,
                max_attempts = ?,
                updated_at = ?
            WHERE mission_id = ?
            """,
            (
                mission.model_dump_json(),
                mission.status.value,
                mission.execution.state.value,
                mission.execution.execution_id,
                mission.execution.worker_id,
                _iso(mission.execution.lease_expires_at),
                mission.execution.attempt_count,
                mission.execution.max_attempts,
                _iso(mission.updated_at),
                mission.mission_id,
            ),
        )

    async def _load_mission(
        self, db: aiosqlite.Connection, mission_id: str
    ) -> Mission | None:
        cursor = await db.execute(
            """
            SELECT data, status, execution_state, execution_id, worker_id,
                   lease_expires_at, attempt_count, max_attempts
            FROM missions WHERE mission_id = ?
            """,
            (mission_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._mission_from_row(row)

    @staticmethod
    def _mission_from_row(row: aiosqlite.Row) -> Mission:
        mission = Mission.model_validate(json.loads(row["data"]))
        mission.status = MissionStatus(row["status"])
        mission.execution.state = ExecutionState(row["execution_state"])
        mission.execution.execution_id = row["execution_id"]
        mission.execution.worker_id = row["worker_id"]
        lease = row["lease_expires_at"]
        mission.execution.lease_expires_at = (
            datetime.fromisoformat(lease) if lease else None
        )
        mission.execution.attempt_count = int(row["attempt_count"])
        mission.execution.max_attempts = int(row["max_attempts"])
        return mission

    async def _get_idempotency(
        self, db: aiosqlite.Connection, idempotency_key: str
    ) -> tuple[str, str] | None:
        cursor = await db.execute(
            """
            SELECT payload_fingerprint, mission_id
            FROM mission_idempotency
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return str(row["payload_fingerprint"]), str(row["mission_id"])
