"""Append bounded governance audit events. Never stores model chain-of-thought."""

from __future__ import annotations

from atlas.domain.enums import (
    ActionRisk,
    ApprovalResolverSource,
    ApprovalStatus,
    EventType,
    GovernanceVerdict,
)
from atlas.domain.models import GovernanceEvent, Mission, MissionEvent, utc_now

_MAX_EVENTS = 40


def append_governance_event(
    mission: Mission,
    *,
    verdict: GovernanceVerdict,
    risk: ActionRisk,
    reason: str,
    fingerprint: str = "",
    decision_id: str | None = None,
    approval_id: str | None = None,
    resolver: str | None = None,
    resolver_source: ApprovalResolverSource | None = None,
    previous_status: ApprovalStatus | None = None,
    new_status: ApprovalStatus | None = None,
    event_type: EventType | None = None,
) -> GovernanceEvent:
    record = GovernanceEvent(
        decision_id=decision_id,
        approval_id=approval_id,
        verdict=verdict,
        risk=risk,
        fingerprint=fingerprint,
        resolver=resolver,
        resolver_source=resolver_source,
        previous_status=previous_status,
        new_status=new_status,
        reason=reason,
        created_at=utc_now(),
    )
    mission.governance_events.append(record)
    mission.governance_events = mission.governance_events[-_MAX_EVENTS:]
    if event_type is not None:
        mission.events.append(
            MissionEvent(
                type=event_type,
                message=reason,
                metadata={
                    "verdict": verdict.value,
                    "risk": risk.value,
                    "approval_id": approval_id,
                    "decision_id": decision_id,
                    "fingerprint": fingerprint,
                    "resolver": resolver,
                    "resolver_source": resolver_source.value if resolver_source else None,
                    "previous_status": previous_status.value if previous_status else None,
                    "new_status": new_status.value if new_status else None,
                },
            )
        )
    return record
