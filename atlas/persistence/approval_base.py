"""Approval repository interface."""

from typing import Protocol

from atlas.domain.models import ApprovalRequest


class ApprovalRepository(Protocol):
    """Abstract persistence for human-approval requests."""

    async def upsert(self, record: ApprovalRequest) -> ApprovalRequest:
        ...

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        ...

    async def find_by_fingerprint(self, fingerprint: str) -> ApprovalRequest | None:
        ...

    async def list_for_mission(self, mission_id: str) -> list[ApprovalRequest]:
        ...

    async def list_expired_pending(self) -> list[ApprovalRequest]:
        ...
