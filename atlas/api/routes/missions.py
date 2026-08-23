"""Mission routes."""

from fastapi import APIRouter, Depends, HTTPException, status

from atlas.api.dependencies import get_mission_service
from atlas.domain.models import (
    CreateMissionRequest,
    CreateMissionResponse,
    MissionDetailResponse,
)
from atlas.services.mission_service import MissionService

router = APIRouter(prefix="/missions", tags=["missions"])


@router.post(
    "",
    response_model=CreateMissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_mission(
    request: CreateMissionRequest,
    service: MissionService = Depends(get_mission_service),
) -> CreateMissionResponse:
    """Create a mission and start background execution."""
    goal = request.goal.strip()
    if not goal:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="goal must be a non-empty string",
        )
    return await service.create_mission(goal)


@router.get("/{mission_id}", response_model=MissionDetailResponse)
async def get_mission(
    mission_id: str,
    service: MissionService = Depends(get_mission_service),
) -> MissionDetailResponse:
    """Retrieve mission status and progress."""
    mission = await service.get_mission(mission_id)
    if mission is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mission '{mission_id}' not found",
        )
    return mission
