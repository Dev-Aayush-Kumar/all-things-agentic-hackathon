"""Local/dev operational routes. Not production control-plane APIs."""

from fastapi import APIRouter, Depends

from atlas.api.dependencies import get_mission_service
from atlas.services.mission_service import MissionService

router = APIRouter(prefix="/ops", tags=["ops"])


@router.post("/recover-missions")
async def recover_missions(
    service: MissionService = Depends(get_mission_service),
) -> dict:
    """Requeue missions with expired leases. Intended for local development and tests."""
    result = await service.recover_abandoned()
    return {
        "recovered_mission_ids": result.recovered_mission_ids,
        "exhausted_mission_ids": result.exhausted_mission_ids,
        "skipped_mission_ids": result.skipped_mission_ids,
    }
