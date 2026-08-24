"""Read-only strategy and experience inspection."""

from atlas.domain.models import (
    ExperienceRecordPublic,
    StrategyListResponse,
    StrategyRecordPublic,
)
from atlas.persistence.learning_base import ExperienceRepository, StrategyRepository


def public_strategy(record) -> StrategyRecordPublic:
    return StrategyRecordPublic(
        strategy_id=record.strategy_id,
        mission_category=record.mission_category,
        dataset_characteristics=record.dataset_characteristics,
        recommended_capabilities=list(record.recommended_capabilities),
        historical_runs=record.historical_runs,
        success_rate=record.success_rate,
        average_efficiency=record.average_efficiency,
        average_evidence_score=record.average_evidence_score,
        confidence=record.confidence,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def public_experience(record) -> ExperienceRecordPublic:
    return ExperienceRecordPublic(
        experience_id=record.experience_id,
        mission_id=record.mission_id,
        mission_category=record.mission_category,
        strategy_signature=record.strategy_signature,
        strategy_steps=list(record.strategy_steps),
        outcome=record.outcome,
        success_score=record.success_score,
        efficiency_score=record.efficiency_score,
        evidence_score=record.evidence_score,
        iterations=record.iterations,
        tool_calls=record.tool_calls,
        model_calls=record.model_calls,
        specialist_tasks=record.specialist_tasks,
        failures=record.failures,
        created_at=record.created_at,
    )


class LearningService:
    def __init__(
        self,
        experience_repository: ExperienceRepository,
        strategy_repository: StrategyRepository,
    ) -> None:
        self._experiences = experience_repository
        self._strategies = strategy_repository

    async def get_strategy(self, strategy_id: str) -> StrategyRecordPublic | None:
        record = await self._strategies.get(strategy_id)
        if record is None:
            return None
        return public_strategy(record)

    async def list_strategies(self, *, limit: int = 50) -> StrategyListResponse:
        records = await self._strategies.list_public(limit=min(limit, 100))
        items = [public_strategy(item) for item in records]
        return StrategyListResponse(items=items, count=len(items))

    async def get_experience_for_mission(
        self, mission_id: str
    ) -> ExperienceRecordPublic | None:
        record = await self._experiences.get_by_mission(mission_id)
        if record is None:
            return None
        return public_experience(record)
