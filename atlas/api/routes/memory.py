"""Memory inspection routes."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from atlas.api.dependencies import get_memory_service
from atlas.domain.models import MemoryListResponse, MemoryRecordPublic
from atlas.services.memory_service import MemoryService

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    memory_type: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    dataset_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryListResponse:
    return await service.list(
        memory_type=memory_type,
        scope=scope,
        dataset_id=dataset_id,
        limit=limit,
    )


@router.get("/{memory_id}", response_model=MemoryRecordPublic)
async def get_memory(
    memory_id: str,
    service: MemoryService = Depends(get_memory_service),
) -> MemoryRecordPublic:
    record = await service.get(memory_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory '{memory_id}' not found",
        )
    return record
