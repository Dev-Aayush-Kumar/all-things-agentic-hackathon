"""Approval routes nested under missions."""

from fastapi import APIRouter, Depends, HTTPException, status

from atlas.api.dependencies import get_approval_service
from atlas.domain.exceptions import ApprovalConflictError, ApprovalNotFoundError
from atlas.domain.models import (
    ApprovalListResponse,
    ApprovalRequestPublic,
    ApprovalResolveRequest,
)
from atlas.services.approval_service import ApprovalService

router = APIRouter(prefix="/missions", tags=["approvals"])


@router.get("/{mission_id}/approvals", response_model=ApprovalListResponse)
async def list_approvals(
    mission_id: str,
    service: ApprovalService = Depends(get_approval_service),
) -> ApprovalListResponse:
    try:
        return await service.list_for_mission(mission_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/{mission_id}/approvals/{approval_id}/approve",
    response_model=ApprovalRequestPublic,
)
async def approve_operation(
    mission_id: str,
    approval_id: str,
    body: ApprovalResolveRequest | None = None,
    service: ApprovalService = Depends(get_approval_service),
) -> ApprovalRequestPublic:
    resolver = body.resolver if body is not None else "human"
    try:
        return await service.approve(mission_id, approval_id, resolver=resolver)
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/{mission_id}/approvals/{approval_id}/reject",
    response_model=ApprovalRequestPublic,
)
async def reject_operation(
    mission_id: str,
    approval_id: str,
    body: ApprovalResolveRequest | None = None,
    service: ApprovalService = Depends(get_approval_service),
) -> ApprovalRequestPublic:
    resolver = body.resolver if body is not None else "human"
    try:
        return await service.reject(mission_id, approval_id, resolver=resolver)
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
