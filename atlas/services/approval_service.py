"""Human approval API. Callers cannot change the persisted operation."""

from __future__ import annotations

from atlas.domain.enums import (
    ApprovalResolverSource,
    ApprovalStatus,
    EventType,
    GovernanceVerdict,
    MissionStatus,
)
from atlas.domain.exceptions import (
    ApprovalConflictError,
    ApprovalNotFoundError,
)
from atlas.domain.models import (
    ApprovalListResponse,
    ApprovalRequest,
    ApprovalRequestPublic,
    Mission,
    public_approval,
    utc_now,
)
from atlas.execution.dispatcher import MissionDispatcher
from atlas.ops.governance.events import append_governance_event
from atlas.ops.governance.lifecycle import apply_resume_queue, maybe_expire
from atlas.persistence.approval_base import ApprovalRepository
from atlas.persistence.base import MissionRepository


class ApprovalService:
    """Resolves durable approval requests. Never lets the caller edit parameters."""

    def __init__(
        self,
        approval_repository: ApprovalRepository,
        mission_repository: MissionRepository,
        dispatcher: MissionDispatcher,
    ) -> None:
        self._approvals = approval_repository
        self._missions = mission_repository
        self._dispatcher = dispatcher

    async def list_for_mission(self, mission_id: str) -> ApprovalListResponse:
        mission = await self._missions.get(mission_id)
        if mission is None:
            raise ApprovalNotFoundError(mission_id)
        await self.expire_due(mission)
        records = await self._approvals.list_for_mission(mission_id)
        items = [public_approval(item) for item in records]
        return ApprovalListResponse(items=items, count=len(items))

    async def pending_for_mission(self, mission: Mission) -> ApprovalRequestPublic | None:
        if not mission.pending_approval_id:
            return None
        record = await self._approvals.get(mission.pending_approval_id)
        if record is None:
            return None
        record = maybe_expire(record)
        return public_approval(record)

    async def approve(
        self,
        mission_id: str,
        approval_id: str,
        *,
        resolver: str = "human",
    ) -> ApprovalRequestPublic:
        record, mission = await self._load_owned(mission_id, approval_id)
        record = await self._persist_if_expired(record, mission)
        if record.status == ApprovalStatus.EXPIRED:
            raise ApprovalConflictError("Expired approval cannot be approved")
        if record.status == ApprovalStatus.REJECTED:
            raise ApprovalConflictError("Rejected approval cannot be approved")
        if record.status == ApprovalStatus.CANCELLED:
            raise ApprovalConflictError("Cancelled approval cannot be approved")
        if record.status in {ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED}:
            return public_approval(record)
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalConflictError(f"Approval is {record.status.value}")
        previous = record.status
        record.status = ApprovalStatus.APPROVED
        record.resolved_at = utc_now()
        record.resolver = resolver.strip() or "human"
        record.resolver_source = ApprovalResolverSource.HUMAN
        await self._approvals.upsert(record)
        append_governance_event(
            mission,
            verdict=GovernanceVerdict.REQUIRE_APPROVAL,
            risk=record.risk,
            reason="Human approved the persisted operation fingerprint",
            fingerprint=record.fingerprint,
            decision_id=record.decision_id,
            approval_id=record.approval_id,
            resolver=record.resolver,
            resolver_source=record.resolver_source,
            previous_status=previous,
            new_status=record.status,
            event_type=EventType.APPROVAL_APPROVED,
        )
        await self._requeue(mission, record)
        return public_approval(record)

    async def reject(
        self,
        mission_id: str,
        approval_id: str,
        *,
        resolver: str = "human",
        reason: str = "Rejected by human approval",
    ) -> ApprovalRequestPublic:
        record, mission = await self._load_owned(mission_id, approval_id)
        record = await self._persist_if_expired(record, mission)
        if record.status == ApprovalStatus.EXPIRED:
            raise ApprovalConflictError("Expired approval cannot be rejected as current")
        if record.status in {ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED}:
            raise ApprovalConflictError("Approved approval cannot be rejected")
        if record.status in {ApprovalStatus.REJECTED, ApprovalStatus.CANCELLED}:
            return public_approval(record)
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalConflictError(f"Approval is {record.status.value}")
        previous = record.status
        record.status = ApprovalStatus.REJECTED
        record.resolved_at = utc_now()
        record.resolver = resolver.strip() or "human"
        record.resolver_source = ApprovalResolverSource.HUMAN
        record.rejection_reason = reason
        await self._approvals.upsert(record)
        append_governance_event(
            mission,
            verdict=GovernanceVerdict.REQUIRE_APPROVAL,
            risk=record.risk,
            reason=reason,
            fingerprint=record.fingerprint,
            decision_id=record.decision_id,
            approval_id=record.approval_id,
            resolver=record.resolver,
            resolver_source=record.resolver_source,
            previous_status=previous,
            new_status=record.status,
            event_type=EventType.APPROVAL_REJECTED,
        )
        await self._requeue(mission, record)
        return public_approval(record)

    async def expire_due(self, mission: Mission | None = None) -> list[ApprovalRequest]:
        expired: list[ApprovalRequest] = []
        pending = await self._approvals.list_expired_pending()
        for record in pending:
            if mission is not None and record.mission_id != mission.mission_id:
                continue
            updated = maybe_expire(record)
            if updated.status != ApprovalStatus.EXPIRED:
                continue
            await self._approvals.upsert(updated)
            target = mission
            if target is None or target.mission_id != updated.mission_id:
                target = await self._missions.get(updated.mission_id)
            if target is None:
                expired.append(updated)
                continue
            append_governance_event(
                target,
                verdict=GovernanceVerdict.REQUIRE_APPROVAL,
                risk=updated.risk,
                reason="Approval request expired",
                fingerprint=updated.fingerprint,
                decision_id=updated.decision_id,
                approval_id=updated.approval_id,
                resolver="system",
                resolver_source=ApprovalResolverSource.SYSTEM,
                previous_status=ApprovalStatus.PENDING,
                new_status=ApprovalStatus.EXPIRED,
                event_type=EventType.APPROVAL_EXPIRED,
            )
            if target.status == MissionStatus.WAITING_FOR_APPROVAL:
                await self._requeue(target, updated)
            else:
                await self._missions.update(target)
            expired.append(updated)
        return expired

    async def _load_owned(
        self, mission_id: str, approval_id: str
    ) -> tuple[ApprovalRequest, Mission]:
        record = await self._approvals.get(approval_id)
        if record is None:
            raise ApprovalNotFoundError(approval_id)
        if record.mission_id != mission_id:
            raise ApprovalNotFoundError(approval_id)
        mission = await self._missions.get(mission_id)
        if mission is None:
            raise ApprovalNotFoundError(approval_id)
        return record, mission

    async def _persist_if_expired(
        self, record: ApprovalRequest, mission: Mission
    ) -> ApprovalRequest:
        previous = record.status
        updated = maybe_expire(record)
        if updated.status == ApprovalStatus.EXPIRED and previous == ApprovalStatus.PENDING:
            await self._approvals.upsert(updated)
            append_governance_event(
                mission,
                verdict=GovernanceVerdict.REQUIRE_APPROVAL,
                risk=updated.risk,
                reason="Approval request expired",
                fingerprint=updated.fingerprint,
                decision_id=updated.decision_id,
                approval_id=updated.approval_id,
                resolver="system",
                resolver_source=ApprovalResolverSource.SYSTEM,
                previous_status=ApprovalStatus.PENDING,
                new_status=ApprovalStatus.EXPIRED,
                event_type=EventType.APPROVAL_EXPIRED,
            )
            await self._missions.update(mission)
        return updated

    async def _requeue(self, mission: Mission, record: ApprovalRequest) -> None:
        apply_resume_queue(mission)
        mission.pending_approval_id = record.approval_id
        already = record.dispatched_at is not None
        if not already:
            record.dispatched_at = utc_now()
            await self._approvals.upsert(record)
        await self._missions.update(mission)
        if not already:
            await self._dispatcher.dispatch(mission.mission_id)
