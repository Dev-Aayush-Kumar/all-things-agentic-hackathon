"""ATLAS governance policy. Independent of the supervisor and decision-makers."""

from __future__ import annotations

from dataclasses import dataclass

from atlas.domain.enums import ActionRisk, GovernanceVerdict, ModelDecisionKind
from atlas.ops.actions.registry import (
    ACTION_FILL_MISSING_VALUES,
    ACTION_REMOVE_DUPLICATES,
    ACTION_CAPABILITIES,
)
from atlas.ops.capabilities import FORBIDDEN_CAPABILITIES
from atlas.ops.decisions import ValidatedDecision, decision_fingerprint
from atlas.ops.external.registry import CAPABILITY_FETCH_URL
from atlas.ops.governance.sanitize import sanitize_parameters
from atlas.ops.registry import (
    ANALYST_CAPABILITIES,
    INVESTIGATOR_CAPABILITIES,
    REPORTER_CAPABILITIES,
)
from atlas.ops.workspace import MissionWorkspace

READONLY_DELEGATE_CAPABILITIES = frozenset(
    ANALYST_CAPABILITIES + INVESTIGATOR_CAPABILITIES + REPORTER_CAPABILITIES
)

REMEDIATION_ACTIONS = frozenset({ACTION_REMOVE_DUPLICATES, ACTION_FILL_MISSING_VALUES})


@dataclass(frozen=True)
class GovernanceDecision:
    """Policy output. Never supplied by the model."""

    verdict: GovernanceVerdict
    risk: ActionRisk
    reason: str
    operation_kind: ModelDecisionKind
    capability: str
    parameters: dict
    fingerprint: str
    requested_operation: str


class GovernancePolicy:
    """Replaceable policy. Memory and strategy never influence the verdict."""

    def evaluate(
        self,
        validated: ValidatedDecision,
        workspace: MissionWorkspace,
    ) -> GovernanceDecision:
        del workspace  # current evidence already constrained validate_decision
        decision = validated.decision
        fingerprint = validated.fingerprint or decision_fingerprint(decision)
        kind = decision.decision
        if kind == ModelDecisionKind.COMPLETE:
            return GovernanceDecision(
                verdict=GovernanceVerdict.AUTO_APPROVE,
                risk=ActionRisk.LOW,
                reason="COMPLETE is always auto-approved",
                operation_kind=kind,
                capability="COMPLETE",
                parameters={},
                fingerprint=fingerprint,
                requested_operation="COMPLETE",
            )
        if kind == ModelDecisionKind.OBSERVE:
            name = decision.tool.name if decision.tool else "unknown"
            params = sanitize_parameters(decision.tool.arguments if decision.tool else {})
            if name in FORBIDDEN_CAPABILITIES:
                return _deny(kind, name, params, fingerprint, "Observation capability is forbidden")
            return GovernanceDecision(
                verdict=GovernanceVerdict.AUTO_APPROVE,
                risk=ActionRisk.LOW,
                reason="Read-only dataset observation is auto-approved",
                operation_kind=kind,
                capability=name,
                parameters=params,
                fingerprint=fingerprint,
                requested_operation=f"OBSERVE:{name}",
            )
        if kind == ModelDecisionKind.DELEGATE:
            return _evaluate_delegate(decision, fingerprint)
        if kind == ModelDecisionKind.ACTION:
            return _evaluate_action(decision, fingerprint)
        if kind == ModelDecisionKind.EXTERNAL:
            return _evaluate_external(decision, fingerprint)
        return _deny(kind, "unknown", {}, fingerprint, "Unsupported decision kind")


def default_governance_policy() -> GovernancePolicy:
    return GovernancePolicy()


def _evaluate_delegate(decision, fingerprint: str) -> GovernanceDecision:
    capabilities = [item.capability for item in decision.tasks]
    params = sanitize_parameters(
        {"capabilities": capabilities, **(decision.tasks[0].inputs if decision.tasks else {})}
    )
    for name in capabilities:
        if name in FORBIDDEN_CAPABILITIES:
            return _deny(
                ModelDecisionKind.DELEGATE,
                name,
                params,
                fingerprint,
                f"Capability '{name}' is forbidden",
            )
        if name in ACTION_CAPABILITIES or name in REMEDIATION_ACTIONS:
            return GovernanceDecision(
                verdict=GovernanceVerdict.REQUIRE_APPROVAL,
                risk=ActionRisk.MEDIUM,
                reason="Remediation cannot be delegated without human approval",
                operation_kind=ModelDecisionKind.DELEGATE,
                capability=name,
                parameters=params,
                fingerprint=fingerprint,
                requested_operation=f"DELEGATE:{name}",
            )
        if name not in READONLY_DELEGATE_CAPABILITIES:
            return _deny(
                ModelDecisionKind.DELEGATE,
                name,
                params,
                fingerprint,
                f"Capability '{name}' is not an allowlisted observation",
            )
    primary = capabilities[0] if capabilities else "DELEGATE"
    return GovernanceDecision(
        verdict=GovernanceVerdict.AUTO_APPROVE,
        risk=ActionRisk.LOW,
        reason="Allowlisted read-only specialist work is auto-approved",
        operation_kind=ModelDecisionKind.DELEGATE,
        capability=primary,
        parameters=params,
        fingerprint=fingerprint,
        requested_operation=f"DELEGATE:{','.join(capabilities)}",
    )


def _evaluate_action(decision, fingerprint: str) -> GovernanceDecision:
    action_type = decision.action.type if decision.action else "unknown"
    params = sanitize_parameters(decision.action.parameters if decision.action else {})
    if action_type in FORBIDDEN_CAPABILITIES:
        return _deny(ModelDecisionKind.ACTION, action_type, params, fingerprint, "Action is forbidden")
    if action_type in REMEDIATION_ACTIONS:
        return GovernanceDecision(
            verdict=GovernanceVerdict.REQUIRE_APPROVAL,
            risk=ActionRisk.MEDIUM,
            reason=f"{action_type} changes the working copy and requires human approval",
            operation_kind=ModelDecisionKind.ACTION,
            capability=action_type,
            parameters=params,
            fingerprint=fingerprint,
            requested_operation=f"ACTION:{action_type}",
        )
    return _deny(
        ModelDecisionKind.ACTION,
        action_type,
        params,
        fingerprint,
        f"Action '{action_type}' is not an allowlisted remediation",
    )


def _evaluate_external(decision, fingerprint: str) -> GovernanceDecision:
    capability = decision.external.capability if decision.external else "unknown"
    params = sanitize_parameters(decision.external.arguments if decision.external else {})
    if capability in FORBIDDEN_CAPABILITIES:
        return _deny(ModelDecisionKind.EXTERNAL, capability, params, fingerprint, "External capability is forbidden")
    if capability == CAPABILITY_FETCH_URL:
        return GovernanceDecision(
            verdict=GovernanceVerdict.AUTO_APPROVE,
            risk=ActionRisk.LOW,
            reason="Allowlisted FETCH_URL already passed destination policy",
            operation_kind=ModelDecisionKind.EXTERNAL,
            capability=capability,
            parameters=params,
            fingerprint=fingerprint,
            requested_operation=f"EXTERNAL:{capability}",
        )
    return _deny(
        ModelDecisionKind.EXTERNAL,
        capability,
        params,
        fingerprint,
        f"External capability '{capability}' is not registered",
    )


def _deny(kind, capability, params, fingerprint, reason) -> GovernanceDecision:
    return GovernanceDecision(
        verdict=GovernanceVerdict.DENY,
        risk=ActionRisk.HIGH,
        reason=reason,
        operation_kind=kind,
        capability=str(capability),
        parameters=params,
        fingerprint=fingerprint,
        requested_operation=f"{kind.value}:{capability}",
    )
