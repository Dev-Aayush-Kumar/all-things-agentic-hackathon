"""Strategy and experience inspection routes."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from atlas.api.dependencies import get_learning_service
from atlas.domain.models import ExperienceRecordPublic, StrategyListResponse, StrategyRecordPublic
from atlas.services.learning_service import LearningService

router = APIRouter(tags=["learning"])


@router.get("/strategies", response_model=StrategyListResponse)
async def list_strategies(
    limit: int = Query(default=50, ge=1, le=100),
    service: LearningService = Depends(get_learning_service),
) -> StrategyListResponse:
    return await service.list_strategies(limit=limit)


@router.get("/strategies/{strategy_id}", response_model=StrategyRecordPublic)
async def get_strategy(
    strategy_id: str,
    service: LearningService = Depends(get_learning_service),
) -> StrategyRecordPublic:
    record = await service.get_strategy(strategy_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy '{strategy_id}' not found",
        )
    return record


@router.get("/experiences/{mission_id}", response_model=ExperienceRecordPublic)
async def get_experience(
    mission_id: str,
    service: LearningService = Depends(get_learning_service),
) -> ExperienceRecordPublic:
    record = await service.get_experience_for_mission(mission_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Experience for mission '{mission_id}' not found",
        )
    return record
