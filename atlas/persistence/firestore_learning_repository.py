"""Firestore (or in-memory document store) experience/strategy persistence."""

from __future__ import annotations

from typing import Any

from atlas.domain.models import ExperienceRecord, StrategyRecord
from atlas.persistence.codec import (
    document_to_experience,
    document_to_strategy,
    experience_to_document,
    strategy_to_document,
)
from atlas.persistence.learning_base import ExperienceRepository, StrategyRepository

EXPERIENCES = "experiences"
EXPERIENCE_FINGERPRINTS = "experience_fingerprints"
EXPERIENCE_MISSIONS = "experience_missions"
STRATEGIES = "strategies"
STRATEGY_FINGERPRINTS = "strategy_fingerprints"


class FirestoreLearningRepository(ExperienceRepository, StrategyRepository):
    """Uses the same document-store API as mission/memory Firestore persistence."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def upsert(self, record):
        if isinstance(record, ExperienceRecord):
            return await self._upsert_experience(record)
        return await self._upsert_strategy(record)

    async def _upsert_experience(self, record: ExperienceRecord) -> ExperienceRecord:
        pointer = await self._store.get_document(EXPERIENCE_FINGERPRINTS, record.fingerprint)
        if pointer and pointer.get("experience_id"):
            current = await self._get_experience(str(pointer["experience_id"]))
            if current is not None:
                record.experience_id = current.experience_id
                record.created_at = current.created_at
        await self._store.set_document(
            EXPERIENCES, record.experience_id, experience_to_document(record)
        )
        await self._store.set_document(
            EXPERIENCE_FINGERPRINTS,
            record.fingerprint,
            {"experience_id": record.experience_id, "fingerprint": record.fingerprint},
        )
        await self._store.set_document(
            EXPERIENCE_MISSIONS,
            record.mission_id,
            {"experience_id": record.experience_id, "mission_id": record.mission_id},
        )
        return record

    async def _upsert_strategy(self, record: StrategyRecord) -> StrategyRecord:
        pointer = await self._store.get_document(STRATEGY_FINGERPRINTS, record.fingerprint)
        if pointer and pointer.get("strategy_id"):
            current = await self._get_strategy(str(pointer["strategy_id"]))
            if current is not None:
                record.strategy_id = current.strategy_id
                record.created_at = current.created_at
        await self._store.set_document(
            STRATEGIES, record.strategy_id, strategy_to_document(record)
        )
        await self._store.set_document(
            STRATEGY_FINGERPRINTS,
            record.fingerprint,
            {"strategy_id": record.strategy_id, "fingerprint": record.fingerprint},
        )
        return record

    async def get(self, record_id: str):
        experience = await self._get_experience(record_id)
        if experience is not None:
            return experience
        return await self._get_strategy(record_id)

    async def _get_experience(self, experience_id: str) -> ExperienceRecord | None:
        document = await self._store.get_document(EXPERIENCES, experience_id)
        if document is None:
            return None
        return document_to_experience(document)

    async def _get_strategy(self, strategy_id: str) -> StrategyRecord | None:
        document = await self._store.get_document(STRATEGIES, strategy_id)
        if document is None:
            return None
        return document_to_strategy(document)

    async def get_by_mission(self, mission_id: str) -> ExperienceRecord | None:
        pointer = await self._store.get_document(EXPERIENCE_MISSIONS, mission_id)
        if pointer is None or not pointer.get("experience_id"):
            return None
        return await self._get_experience(str(pointer["experience_id"]))

    async def find_by_fingerprint(self, fingerprint: str):
        pointer = await self._store.get_document(EXPERIENCE_FINGERPRINTS, fingerprint)
        if pointer and pointer.get("experience_id"):
            found = await self._get_experience(str(pointer["experience_id"]))
            if found is not None:
                return found
        pointer = await self._store.get_document(STRATEGY_FINGERPRINTS, fingerprint)
        if pointer is None or not pointer.get("strategy_id"):
            return None
        return await self._get_strategy(str(pointer["strategy_id"]))

    async def list_candidates(self, *, limit: int = 100) -> list[StrategyRecord]:
        rows = await self._store.list_documents(STRATEGIES)
        records = [document_to_strategy(data) for _, data in rows]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit]

    async def list_public(self, *, limit: int = 50) -> list[StrategyRecord]:
        return await self.list_candidates(limit=limit)
