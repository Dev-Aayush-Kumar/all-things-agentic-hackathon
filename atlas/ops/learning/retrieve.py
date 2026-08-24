"""Bounded deterministic strategy retrieval. Replaceable later by a better ranker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from atlas.config.settings import Settings
from atlas.domain.enums import MissionCategory
from atlas.domain.models import DatasetCharacteristics, Mission, StrategyRecord
from atlas.ops.learning.signatures import (
    categories_related,
    classify_goal,
    dataset_characteristics,
    dataset_signature,
)

if TYPE_CHECKING:
    from atlas.persistence.learning_base import StrategyRepository

TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


@dataclass(frozen=True)
class StrategyQuery:
    goal: str
    category: MissionCategory
    characteristics: DatasetCharacteristics
    limit: int = 3


class StrategyRetriever:
    """Lexical/category/dataset overlap. Does not call Gemini or embeddings."""

    def __init__(self, repository: StrategyRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    async def retrieve(self, query: StrategyQuery) -> list[StrategyRecord]:
        if not self._settings.strategy_enabled:
            return []
        limit = min(query.limit, self._settings.strategy_max_retrieval)
        candidates = await self._repository.list_candidates(limit=100)
        goal_tokens = _tokens(query.goal)
        scored: list[tuple[float, StrategyRecord]] = []
        for record in candidates:
            if record.confidence < self._settings.strategy_min_confidence:
                continue
            score = _score(record, query, goal_tokens)
            if score <= 0:
                continue
            scored.append((score, record))
        scored.sort(
            key=lambda item: (
                -item[0],
                -item[1].confidence,
                -item[1].historical_runs,
                item[1].updated_at,
            )
        )
        return [record for _, record in scored[:limit]]

    async def retrieve_for_mission(self, mission: Mission) -> list[StrategyRecord]:
        signature = dataset_signature(mission)
        query = StrategyQuery(
            goal=mission.goal,
            category=classify_goal(mission.goal),
            characteristics=dataset_characteristics(signature),
            limit=self._settings.strategy_max_retrieval,
        )
        return await self.retrieve(query)


def _score(record: StrategyRecord, query: StrategyQuery, goal_tokens: set[str]) -> float:
    score = 0.0
    if record.mission_category == query.category:
        score += 3.0
    elif categories_related(record.mission_category, query.category):
        score += 1.5
    else:
        return 0.0
    chars = record.dataset_characteristics
    if chars.has_numeric == query.characteristics.has_numeric:
        score += 1.0
    if chars.missingness == query.characteristics.missingness:
        score += 0.5
    if chars.row_bucket == query.characteristics.row_bucket:
        score += 0.25
    haystack = _tokens(" ".join(record.recommended_capabilities)) | {
        record.mission_category.value.lower(),
        "duplicate",
        "duplicates",
        "outlier",
        "outliers",
        "missing",
        "quality",
    }
    overlap = len(goal_tokens & haystack)
    score += overlap
    if {"duplicate", "duplicates"} & goal_tokens and "analyze_duplicates" in record.recommended_capabilities:
        score += 1.5
    score += record.confidence
    score += min(1.0, record.historical_runs / 10)
    score += record.success_rate
    return score


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall((text or "").lower()))
