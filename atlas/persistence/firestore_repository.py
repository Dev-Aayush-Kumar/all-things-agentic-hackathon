"""Firestore-backed mission repository.

Uses an explicit document codec and transactional claim/lease updates.
The store is Firestore in production and an in-memory double in tests.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.exceptions import IdempotencyConflictError, StaleExecutionError
from atlas.domain.models import Mission, MissionEvent, utc_now
from atlas.execution.context import ExecutionContext
from atlas.persistence.codec import document_to_mission, mission_to_document
from atlas.persistence.lease_policy import (
    apply_claim,
    is_claimable,
    is_owned,
    is_recoverable,
)
from atlas.persistence.firestore_store import DATASETS, IDEMPOTENCY, MISSIONS

_LEASE_CLEAR_STATES = {
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.EXHAUSTED,
}


class FirestoreMissionRepository:
    """Mission repository persisted in Firestore documents."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def create(self, mission: Mission) -> Mission:
        await self._store.set_document(
            MISSIONS, mission.mission_id, mission_to_document(mission)
        )
        return mission

    async def create_idempotent(
        self,
        mission: Mission,
        idempotency_key: str,
        payload_fingerprint: str,
    ) -> tuple[Mission, bool]:
        key_id = _idempotency_id(idempotency_key)

        async def txn(tx: Any) -> tuple[Mission, bool]:
            existing = await tx.get(IDEMPOTENCY, key_id)
            if existing is not None:
                if existing.get("payload_fingerprint") != payload_fingerprint:
                    raise IdempotencyConflictError(idempotency_key)
                stored_id = str(existing["mission_id"])
                data = await tx.get(MISSIONS, stored_id)
                if data is None:
                    raise RuntimeError(
                        f"Idempotency key '{idempotency_key}' points to a missing mission"
                    )
                return document_to_mission(data), True
            tx.set(
                IDEMPOTENCY,
                key_id,
                {
                    "idempotency_key": idempotency_key,
                    "payload_fingerprint": payload_fingerprint,
                    "mission_id": mission.mission_id,
                    "created_at": mission.created_at.isoformat(),
                },
            )
            tx.set(MISSIONS, mission.mission_id, mission_to_document(mission))
            return mission, False

        return await self._store.run_transaction(txn)

    async def get(self, mission_id: str) -> Mission | None:
        data = await self._store.get_document(MISSIONS, mission_id)
        if data is None:
            return None
        return document_to_mission(data)

    async def update(self, mission: Mission) -> Mission:
        mission.touch()
        await self._store.set_document(
            MISSIONS, mission.mission_id, mission_to_document(mission)
        )
        return mission

    async def update_owned(
        self, mission: Mission, context: ExecutionContext
    ) -> Mission:
        now = utc_now()
        mission.touch()

        async def txn(tx: Any) -> Mission:
            data = await tx.get(MISSIONS, mission.mission_id)
            if data is None:
                raise StaleExecutionError(mission.mission_id)
            current = document_to_mission(data)
            if not is_owned(current, context, now):
                raise StaleExecutionError(mission.mission_id)
            if mission.execution.state in _LEASE_CLEAR_STATES:
                mission.execution.lease_expires_at = None
                mission.execution.worker_id = None
            tx.set(MISSIONS, mission.mission_id, mission_to_document(mission))
            return mission

        return await self._store.run_transaction(txn)

    async def claim(
        self,
        mission_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> Mission | None:
        current = now or utc_now()

        async def txn(tx: Any) -> Mission | None:
            data = await tx.get(MISSIONS, mission_id)
            if data is None:
                return None
            mission = document_to_mission(data)
            if not is_claimable(mission, current):
                return None
            apply_claim(mission, worker_id, lease_seconds=lease_seconds, now=current)
            tx.set(MISSIONS, mission_id, mission_to_document(mission))
            return mission

        return await self._store.run_transaction(txn)

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

        async def txn(tx: Any) -> bool:
            data = await tx.get(MISSIONS, mission_id)
            if data is None:
                return False
            mission = document_to_mission(data)
            if not is_owned(mission, context, current):
                return False
            mission.execution.lease_expires_at = expires
            mission.execution.heartbeat_at = current
            mission.touch()
            tx.set(MISSIONS, mission_id, mission_to_document(mission))
            return True

        return await self._store.run_transaction(txn)

    async def list_recoverable(self, now: datetime | None = None) -> list[Mission]:
        current = now or utc_now()
        documents = await self._store.list_documents(MISSIONS)
        recoverable: list[Mission] = []
        for _doc_id, data in documents:
            mission = document_to_mission(data)
            if is_recoverable(mission, current):
                recoverable.append(mission)
        return recoverable

    async def requeue_expired(
        self, mission_id: str, now: datetime | None = None
    ) -> Mission | None:
        current = now or utc_now()

        async def txn(tx: Any) -> Mission | None:
            data = await tx.get(MISSIONS, mission_id)
            if data is None:
                return None
            mission = document_to_mission(data)
            if not is_recoverable(mission, current):
                return None
            if mission.execution.attempt_count >= mission.execution.max_attempts:
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
            tx.set(MISSIONS, mission_id, mission_to_document(mission))
            return mission

        return await self._store.run_transaction(txn)

    async def exhaust_expired(
        self, mission_id: str, now: datetime | None = None
    ) -> Mission | None:
        current = now or utc_now()

        async def txn(tx: Any) -> Mission | None:
            data = await tx.get(MISSIONS, mission_id)
            if data is None:
                return None
            mission = document_to_mission(data)
            if not is_recoverable(mission, current):
                return None
            if mission.execution.attempt_count < mission.execution.max_attempts:
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
            tx.set(MISSIONS, mission_id, mission_to_document(mission))
            return mission

        return await self._store.run_transaction(txn)

    async def delete(self, mission_id: str) -> None:
        await self._store.delete_document(MISSIONS, mission_id)


class FirestoreDatasetRepository:
    """Dataset metadata stored in Firestore."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def create(self, record):
        from atlas.persistence.codec import dataset_to_document

        await self._store.set_document(
            DATASETS, record.dataset_id, dataset_to_document(record)
        )
        return record

    async def get(self, dataset_id: str):
        from atlas.persistence.codec import document_to_dataset

        data = await self._store.get_document(DATASETS, dataset_id)
        if data is None:
            return None
        return document_to_dataset(data)


def _idempotency_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
