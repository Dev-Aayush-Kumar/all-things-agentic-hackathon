"""Pause, resume, and approval-request helpers. Policy stays in GovernancePolicy."""

from __future__ import annotations

from datetime import timedelta

from atlas.config.settings import Settings
from atlas.domain.enums import (
    ActionStatus,
    AgentPhase,
    ApprovalResolverSource,
    ApprovalStatus,
    EventType,
    ExecutionState,
    GovernanceVerdict,
    MissionStatus,
)
from atlas.domain.models import (
    ActionRecord,
    ApprovalRequest,
    Mission,
    utc_now,
)
from atlas.ops.actions.registry import make_idempotency_key
from atlas.ops.decisions import ValidatedDecision
from atlas.ops.governance.events import append_governance_event
from atlas.ops.governance.policy import GovernanceDecision
from atlas.persistence.approval_base import ApprovalRepository


def approval_fingerprint(mission_id: str, decision_fingerprint: str) -> str:
    return f"{mission_id}:{decision_fingerprint}"


def apply_waiting_state(mission: Mission, approval: ApprovalRequest) -> None:
    mission.status = MissionStatus.WAITING_FOR_APPROVAL
    mission.current_phase = AgentPhase.WAITING_FOR_APPROVAL
    mission.pending_approval_id = approval.approval_id
    mission.execution.state = ExecutionState.WAITING_FOR_APPROVAL
    mission.execution.worker_id = None
    mission.execution.execution_id = None
    mission.execution.lease_expires_at = None
    mission.execution.last_error = None
    mission.touch()


def apply_resume_queue(mission: Mission) -> None:
    mission.status = MissionStatus.EXECUTING
    mission.current_phase = AgentPhase.REASONING
    mission.execution.state = ExecutionState.QUEUED
    mission.execution.resume_without_attempt = True
    mission.execution.worker_id = None
    mission.execution.execution_id = None
    mission.execution.lease_expires_at = None
    mission.execution.last_error = None
    mission.touch()


async def persist_or_reuse_approval(
    repository: ApprovalRepository,
    mission: Mission,
    validated: ValidatedDecision,
    governance: GovernanceDecision,
    settings: Settings,
    *,
    decision_id: str | None,
) -> ApprovalRequest:
    fingerprint = approval_fingerprint(mission.mission_id, governance.fingerprint)
    existing = await repository.find_by_fingerprint(fingerprint)
    if existing is not None and existing.mission_id == mission.mission_id:
        if existing.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
            return existing
    ttl = max(1.0, float(settings.approval_ttl_seconds))
    now = utc_now()
    record = ApprovalRequest(
        mission_id=mission.mission_id,
        execution_id=mission.execution.execution_id,
        decision_id=decision_id,
        requested_operation=governance.requested_operation,
        operation_kind=governance.operation_kind,
        capability=governance.capability,
        parameters=dict(governance.parameters),
        reason=governance.reason,
        risk=governance.risk,
        policy_verdict=governance.verdict,
        status=ApprovalStatus.PENDING,
        fingerprint=fingerprint,
        decision_snapshot=validated.decision.model_dump(mode="json"),
        requested_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    stored = await repository.upsert(record)
    append_governance_event(
        mission,
        verdict=GovernanceVerdict.REQUIRE_APPROVAL,
        risk=governance.risk,
        reason=governance.reason,
        fingerprint=fingerprint,
        decision_id=decision_id,
        approval_id=stored.approval_id,
        new_status=ApprovalStatus.PENDING,
        event_type=EventType.APPROVAL_REQUESTED,
    )
    return stored


def record_rejected_action(mission: Mission, approval: ApprovalRequest) -> None:
    """Mark the proposed remediation as considered so it is not re-queued forever."""
    if approval.operation_kind.value != "ACTION":
        return
    input_version = (
        mission.working_copy.current_version if mission.working_copy is not None else 0
    )
    key = make_idempotency_key(
        mission_id=mission.mission_id,
        action_type=approval.capability,
        parameters=dict(approval.parameters),
        input_version=input_version,
    )
    if any(item.idempotency_key == key for item in mission.actions):
        for item in mission.actions:
            if item.idempotency_key == key and item.status == ActionStatus.PROPOSED:
                item.status = ActionStatus.FAILED
                item.error = approval.rejection_reason or "Rejected by human approval"
                item.completed_at = utc_now()
        return
    mission.actions.append(
        ActionRecord(
            mission_id=mission.mission_id,
            agent_id="atlas.remediator",
            action_type=approval.capability,
            objective=approval.requested_operation,
            parameters=dict(approval.parameters),
            status=ActionStatus.FAILED,
            error=approval.rejection_reason or "Rejected by human approval",
            idempotency_key=key,
            input_version=input_version,
            completed_at=utc_now(),
        )
    )


def mark_expired(record: ApprovalRequest) -> ApprovalRequest:
    record.status = ApprovalStatus.EXPIRED
    record.resolved_at = utc_now()
    record.resolver = "system"
    record.resolver_source = ApprovalResolverSource.SYSTEM
    record.rejection_reason = "Approval request expired"
    return record


def maybe_expire(record: ApprovalRequest) -> ApprovalRequest:
    if record.status == ApprovalStatus.PENDING and record.expires_at <= utc_now():
        return mark_expired(record)
    return record
