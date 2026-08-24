"""Bounded lexical memory retrieval. Replaceable later by a vector backend."""

from __future__ import annotations

import re
from dataclasses import dataclass

from atlas.config.settings import Settings
from atlas.domain.enums import MemoryScope
from atlas.domain.models import MemoryRecord
from atlas.persistence.memory_base import MemoryRepository

TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


@dataclass(frozen=True)
class MemoryQuery:
    goal: str
    dataset_id: str | None = None
    mission_id: str | None = None
    limit: int = 5


class MemoryRetriever:
    """Deterministic tag/token overlap. Does not call Gemini or embeddings."""

    def __init__(self, repository: MemoryRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    async def retrieve(self, query: MemoryQuery) -> list[MemoryRecord]:
        if not self._settings.memory_enabled:
            return []
        limit = min(query.limit, self._settings.memory_max_retrieval)
        candidates = await self._repository.list_candidates(limit=200)
        goal_tokens = _tokens(query.goal)
        scored: list[tuple[float, MemoryRecord]] = []
        for record in candidates:
            if record.confidence < self._settings.memory_min_confidence:
                continue
            if not _in_scope(record, query):
                continue
            score = _score(record, goal_tokens)
            if score <= 0:
                continue
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], -item[1].confidence, item[1].updated_at))
        return [record for _, record in scored[:limit]]


def _in_scope(record: MemoryRecord, query: MemoryQuery) -> bool:
    if record.scope == MemoryScope.GLOBAL:
        return True
    if record.scope == MemoryScope.DATASET:
        return bool(query.dataset_id) and record.scope_ref == query.dataset_id
    if record.scope == MemoryScope.MISSION:
        return bool(query.mission_id) and record.scope_ref == query.mission_id
    return False


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall((text or "").lower()))


def _score(record: MemoryRecord, goal_tokens: set[str]) -> float:
    haystack = _tokens(record.content) | {tag.lower() for tag in record.tags} | {
        record.type.value.lower()
    }
    overlap = len(goal_tokens & haystack)
    tag_bonus = 1.5 * len(goal_tokens & {tag.lower() for tag in record.tags})
    type_bonus = 0.5 if record.type.value.lower() in goal_tokens else 0.0
    if not overlap and not tag_bonus:
        if {"outlier", "outliers", "extreme", "numeric", "duplicate", "duplicates", "quality", "survey"} & goal_tokens:
            if {"outliers", "duplicates", "investigation", "procedure", "survey"} & set(record.tags):
                return 1.0 + record.confidence
        return 0.0
    return overlap + tag_bonus + type_bonus + record.confidence
