"""Post-completion experience recording and strategy aggregation.

Failures never fail the mission. Gemini cannot write these records.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from atlas.config.settings import Settings
from atlas.domain.enums import EventType
from atlas.domain.exceptions import StrategyValidationError
from atlas.domain.models import Mission, MissionEvent
from atlas.ops.learning.evaluate import evaluate_mission
from atlas.ops.learning.policy import merge_strategy, strategy_from_experience

if TYPE_CHECKING:
    from atlas.persistence.learning_base import ExperienceRepository, StrategyRepository

logger = logging.getLogger(__name__)


async def record_experience_and_strategy(
    mission: Mission,
    experience_repository: ExperienceRepository,
    strategy_repository: StrategyRepository,
    settings: Settings,
) -> tuple:
    """Evaluate, persist, and aggregate. Returns (experience, strategy_or_none)."""
    if not settings.strategy_enabled:
        return None, None
    try:
        experience = evaluate_mission(mission, settings)
    except Exception as exc:
        logger.exception("Experience evaluation failed mission=%s", mission.mission_id)
        _add_event(
            mission,
            EventType.STRATEGY_EXTRACTION_FAILED,
            "Experience evaluation failed",
            {"error": str(exc)},
        )
        return None, None
    try:
        existing = await experience_repository.find_by_fingerprint(experience.fingerprint)
        stored = await experience_repository.upsert(experience)
        event = EventType.EXPERIENCE_MERGED if existing is not None else EventType.EXPERIENCE_RECORDED
        _add_event(
            mission,
            event,
            "Experience recorded" if existing is None else "Experience updated in place",
            {
                "experience_id": stored.experience_id,
                "outcome": stored.outcome.value,
                "success_score": stored.success_score,
            },
        )
    except Exception as exc:
        logger.exception("Experience persistence failed mission=%s", mission.mission_id)
        _add_event(
            mission,
            EventType.STRATEGY_EXTRACTION_FAILED,
            "Experience persistence failed",
            {"error": str(exc)},
        )
        return None, None
    try:
        candidate = strategy_from_experience(stored)
        if candidate is None:
            _add_event(
                mission,
                EventType.STRATEGY_REJECTED,
                "No allowlisted strategy could be derived",
                {"experience_id": stored.experience_id},
            )
            return stored, None
        existing_strategy = await strategy_repository.find_by_fingerprint(candidate.fingerprint)
        if existing_strategy is not None:
            merged = merge_strategy(existing_strategy, stored)
            saved = await strategy_repository.upsert(merged)
        else:
            saved = await strategy_repository.upsert(candidate)
        _add_event(
            mission,
            EventType.STRATEGY_UPDATED,
            "Strategy aggregate updated",
            {
                "strategy_id": saved.strategy_id,
                "confidence": saved.confidence,
                "historical_runs": saved.historical_runs,
            },
        )
        return stored, saved
    except StrategyValidationError as exc:
        _add_event(
            mission,
            EventType.STRATEGY_REJECTED,
            "Strategy rejected",
            {"error": str(exc)},
        )
        return stored, None
    except Exception as exc:
        logger.exception("Strategy aggregation failed mission=%s", mission.mission_id)
        _add_event(
            mission,
            EventType.STRATEGY_EXTRACTION_FAILED,
            "Strategy aggregation failed",
            {"error": str(exc)},
        )
        return stored, None


def _add_event(mission: Mission, event_type: EventType, message: str, metadata: dict) -> None:
    mission.events.append(MissionEvent(type=event_type, message=message, metadata=metadata))
    mission.touch()
