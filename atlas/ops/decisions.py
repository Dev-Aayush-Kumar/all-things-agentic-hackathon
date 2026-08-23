"""Validate typed model decisions before ATLAS executes anything."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from atlas.agent.tools import INSPECT_COLUMN, INVESTIGATION_TOOLS, TOOL_ALLOWED_ARGS
from atlas.domain.enums import ActionStatus, ModelDecisionKind, StepStatus
from atlas.domain.exceptions import ModelDecisionError
from atlas.domain.models import (
    ModelDecision,
    ProposedActionRequest,
    ProposedExternalRequest,
    ProposedObservation,
    ProposedTask,
    SpecialistFollowUp,
)
from atlas.ops.actions.registry import (
    ACTION_CAPABILITIES,
    CAPABILITY_FILL_MISSING,
    CAPABILITY_REMOVE_DUPLICATES,
    ActionRegistry,
    default_action_registry,
)
from atlas.ops.capabilities import (
    FORBIDDEN_CAPABILITIES,
    resolve_action_type,
    resolve_external_capability,
    resolve_observation_name,
    resolve_specialist_capability,
)
from atlas.ops.external.policy import authorize_external_tool, validate_external_arguments
from atlas.ops.external.registry import default_external_registry
from atlas.ops.registry import AgentRegistry, default_registry
from atlas.ops.workspace import MissionWorkspace


@dataclass(frozen=True)
class ValidatedDecision:
    """A decision that passed schema, catalog, and registry checks."""

    decision: ModelDecision
    follow_ups: list[SpecialistFollowUp]
    fingerprint: str


def parse_model_decision(payload: Any) -> ModelDecision:
    """Parse a dict/JSON object into ModelDecision. Never guesses a kind."""
    if isinstance(payload, ModelDecision):
        _assert_payload_matches_kind(payload)
        return payload
    if not isinstance(payload, dict):
        raise ModelDecisionError("Model decision must be a JSON object")
    if "decision" not in payload:
        raise ModelDecisionError("Model decision is missing the 'decision' field")
    try:
        decision = ModelDecision.model_validate(payload)
    except Exception as exc:
        raise ModelDecisionError(f"Malformed model decision: {exc}") from exc
    _assert_payload_matches_kind(decision)
    return decision


def validate_decision(
    decision: ModelDecision,
    workspace: MissionWorkspace,
    *,
    registry: AgentRegistry | None = None,
    action_registry: ActionRegistry | None = None,
) -> ValidatedDecision:
    """Reject unknown/forbidden work. Does not execute."""
    registry = registry or workspace.registry or default_registry()
    action_registry = action_registry or default_action_registry()
    _assert_payload_matches_kind(decision)
    follow_ups: list[SpecialistFollowUp] = []

    if decision.decision == ModelDecisionKind.DELEGATE:
        if not decision.tasks:
            raise ModelDecisionError("DELEGATE requires at least one task")
        for task in decision.tasks:
            follow_ups.append(_validate_task(task, workspace, registry))
    elif decision.decision == ModelDecisionKind.OBSERVE:
        if decision.tool is None:
            raise ModelDecisionError("OBSERVE requires a tool object")
        follow_ups.append(_validate_observation(decision.tool, workspace, registry, decision.reason))
    elif decision.decision == ModelDecisionKind.ACTION:
        if decision.action is None:
            raise ModelDecisionError("ACTION requires an action object")
        follow_ups.append(
            _validate_action(decision.action, workspace, action_registry, decision.reason)
        )
    elif decision.decision == ModelDecisionKind.EXTERNAL:
        if decision.external is None:
            raise ModelDecisionError("EXTERNAL requires an external object")
        _validate_external(decision.external, workspace)
    elif decision.decision == ModelDecisionKind.COMPLETE:
        if workspace.mission.dataset_profile is None:
            raise ModelDecisionError("COMPLETE is not allowed before a dataset profile exists")
    else:
        raise ModelDecisionError(f"Unsupported decision '{decision.decision}'")

    plan = workspace.mission.delegation_plan
    if plan is not None:
        pending = sum(
            1
            for task in plan.tasks
            if task.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
        )
        projected = pending + len(follow_ups)
        if len(plan.tasks) + len(follow_ups) > workspace.settings.max_specialist_tasks:
            raise ModelDecisionError("Specialist task budget exhausted")
        if projected > workspace.settings.max_specialist_tasks:
            raise ModelDecisionError("Specialist task budget exhausted")

    return ValidatedDecision(
        decision=decision,
        follow_ups=follow_ups,
        fingerprint=decision_fingerprint(decision),
    )


def decision_fingerprint(decision: ModelDecision) -> str:
    payload = {
        "decision": decision.decision.value,
        "tasks": [
            {"capability": item.capability, "inputs": item.inputs}
            for item in decision.tasks
        ],
        "tool": decision.tool.model_dump() if decision.tool else None,
        "action": decision.action.model_dump() if decision.action else None,
        "external": decision.external.model_dump() if decision.external else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assert_payload_matches_kind(decision: ModelDecision) -> None:
    kind = decision.decision
    if kind == ModelDecisionKind.DELEGATE and not decision.tasks:
        raise ModelDecisionError("DELEGATE requires tasks")
    if kind == ModelDecisionKind.OBSERVE and decision.tool is None:
        raise ModelDecisionError("OBSERVE requires tool")
    if kind == ModelDecisionKind.ACTION and decision.action is None:
        raise ModelDecisionError("ACTION requires action")
    if kind == ModelDecisionKind.EXTERNAL and decision.external is None:
        raise ModelDecisionError("EXTERNAL requires external")
    if kind == ModelDecisionKind.COMPLETE:
        if (
            decision.tasks
            or decision.tool is not None
            or decision.action is not None
            or decision.external is not None
        ):
            raise ModelDecisionError("COMPLETE cannot include tasks, tools, actions, or external")
    if kind != ModelDecisionKind.DELEGATE and decision.tasks:
        raise ModelDecisionError(f"{kind.value} cannot include tasks")
    if kind != ModelDecisionKind.OBSERVE and decision.tool is not None:
        raise ModelDecisionError(f"{kind.value} cannot include a tool")
    if kind != ModelDecisionKind.ACTION and decision.action is not None:
        raise ModelDecisionError(f"{kind.value} cannot include an action")
    if kind != ModelDecisionKind.EXTERNAL and decision.external is not None:
        raise ModelDecisionError(f"{kind.value} cannot include an external request")


def _reject_forbidden(name: str) -> None:
    token = name.strip()
    if token in FORBIDDEN_CAPABILITIES or token.upper() in FORBIDDEN_CAPABILITIES:
        raise ModelDecisionError(f"Capability '{name}' is forbidden")


def _validate_task(
    task: ProposedTask,
    workspace: MissionWorkspace,
    registry: AgentRegistry,
) -> SpecialistFollowUp:
    _reject_forbidden(task.capability)
    capability = resolve_specialist_capability(task.capability)
    if capability is None:
        raise ModelDecisionError(f"Unknown capability '{task.capability}'")
    if capability in ACTION_CAPABILITIES:
        raise ModelDecisionError(
            "Actions must use decision=ACTION; they cannot be delegated as specialists"
        )
    try:
        registry.match(capability)
    except Exception as exc:
        raise ModelDecisionError(f"Unknown specialist capability '{task.capability}'") from exc
    arguments = dict(task.inputs)
    if capability == INSPECT_COLUMN or capability == "investigate_column":
        column = arguments.get("column_name")
        if not isinstance(column, str) or not column:
            raise ModelDecisionError("inspect/investigate column requires column_name")
        _assert_column_exists(workspace, column)
    if capability in INVESTIGATION_TOOLS:
        _assert_tool_args(capability, arguments)
    return SpecialistFollowUp(
        capability=capability,
        objective=task.objective or f"Execute {capability}",
        arguments=arguments,
        reason=task.objective or f"Model delegated {capability}",
        critical=capability == "synthesize_report",
    )


def _validate_observation(
    tool: ProposedObservation,
    workspace: MissionWorkspace,
    registry: AgentRegistry,
    reason: str,
) -> SpecialistFollowUp:
    _reject_forbidden(tool.name)
    name = resolve_observation_name(tool.name)
    if name is None:
        raise ModelDecisionError(f"Unknown observation tool '{tool.name}'")
    try:
        registry.match(name)
        registry.authorize_tool(registry.match(name).id, name)
    except Exception as exc:
        raise ModelDecisionError(f"Observation '{tool.name}' is not authorized") from exc
    arguments = dict(tool.arguments)
    _assert_tool_args(name, arguments)
    if name == INSPECT_COLUMN:
        column = arguments.get("column_name")
        if not isinstance(column, str) or not column:
            raise ModelDecisionError("inspect_column requires column_name")
        _assert_column_exists(workspace, column)
    return SpecialistFollowUp(
        capability=name,
        objective=reason or f"Observe with {name}",
        arguments=arguments,
        reason=reason or f"Model requested observation {name}",
    )


def _validate_action(
    action: ProposedActionRequest,
    workspace: MissionWorkspace,
    action_registry: ActionRegistry,
    reason: str,
) -> SpecialistFollowUp:
    _reject_forbidden(action.type)
    action_type = resolve_action_type(action.type)
    if action_type is None:
        raise ModelDecisionError(f"Unknown action '{action.type}'")
    completed = sum(
        1
        for item in workspace.mission.actions
        if item.status in {ActionStatus.VERIFIED, ActionStatus.COMPLETED}
    )
    if completed >= workspace.settings.max_mission_actions:
        raise ModelDecisionError("Action budget exhausted")
    try:
        action_registry.get(action_type)
        action_registry.validate_parameters(action_type, action.parameters or {})
    except ModelDecisionError:
        raise
    except Exception as exc:
        raise ModelDecisionError(f"Action '{action.type}' rejected: {exc}") from exc
    capability = (
        CAPABILITY_REMOVE_DUPLICATES
        if action_type == "REMOVE_DUPLICATES"
        else CAPABILITY_FILL_MISSING
    )
    return SpecialistFollowUp(
        capability=capability,
        objective=reason or f"Execute {action_type}",
        arguments={"action_type": action_type, "parameters": dict(action.parameters or {})},
        reason=reason or f"Model proposed {action_type}",
    )


def _validate_external(request: ProposedExternalRequest, workspace: MissionWorkspace) -> None:
    _reject_forbidden(request.capability)
    capability = resolve_external_capability(request.capability)
    if capability is None:
        raise ModelDecisionError(f"Unknown external capability '{request.capability}'")
    try:
        default_external_registry().get(capability)
        safe = validate_external_arguments(capability, dict(request.arguments))
        authorize_external_tool(capability, safe, workspace)
    except ModelDecisionError:
        raise
    except Exception as exc:
        raise ModelDecisionError(f"External capability '{request.capability}' rejected: {exc}") from exc


def _assert_tool_args(tool_name: str, arguments: dict[str, Any]) -> None:
    allowed = TOOL_ALLOWED_ARGS.get(tool_name)
    if allowed is None:
        return
    extra = set(arguments) - allowed - {"adaptive", "reobserve", "working_version"}
    if extra:
        raise ModelDecisionError(
            f"Tool '{tool_name}' rejected unknown arguments: {sorted(extra)}"
        )


def _assert_column_exists(workspace: MissionWorkspace, column: str) -> None:
    if column not in workspace.tool_context.frame.columns:
        raise ModelDecisionError(f"Column '{column}' is not in the bound dataset")
