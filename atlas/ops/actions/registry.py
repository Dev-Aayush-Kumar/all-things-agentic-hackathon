"""Allowlisted action contracts. Unknown, unauthorized, or malformed actions fail."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from atlas.domain.enums import ActionRisk
from atlas.domain.exceptions import (
    ActionAuthorizationError,
    ActionValidationError,
    UnknownActionError,
)
from atlas.domain.models import ActionDescriptor, ActionVerification
from atlas.ops.actions.remediations import (
    measure_duplicates,
    measure_missing,
    measure_shape,
    identity_transform,
    reject_unverified,
    transform_fill_missing,
    transform_remove_duplicates,
    verify_fill_missing,
    verify_remove_duplicates,
)
from atlas.ops.registry import REMEDIATOR_ID

ACTION_REMOVE_DUPLICATES = "REMOVE_DUPLICATES"
ACTION_FILL_MISSING_VALUES = "FILL_MISSING_VALUES"

CAPABILITY_REMOVE_DUPLICATES = "remove_duplicates"
CAPABILITY_FILL_MISSING = "fill_missing_values"
CAPABILITY_EXECUTE_REMEDIATION = "execute_remediation"

ACTION_CAPABILITIES = frozenset(
    {
        CAPABILITY_REMOVE_DUPLICATES,
        CAPABILITY_FILL_MISSING,
        CAPABILITY_EXECUTE_REMEDIATION,
    }
)

CAPABILITY_TO_ACTION = {
    CAPABILITY_REMOVE_DUPLICATES: ACTION_REMOVE_DUPLICATES,
    CAPABILITY_FILL_MISSING: ACTION_FILL_MISSING_VALUES,
}

MeasureFn = Callable[[pd.DataFrame, dict[str, Any]], dict[str, Any]]
TransformFn = Callable[[pd.DataFrame, dict[str, Any]], pd.DataFrame]
VerifyFn = Callable[[dict[str, Any], pd.DataFrame, dict[str, Any]], ActionVerification]


@dataclass(frozen=True)
class ActionSpec:
    """Registered action contract. Execution functions stay inside ATLAS."""

    action_type: str
    description: str
    required_parameters: tuple[str, ...] = ()
    optional_parameters: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    risk: ActionRisk = ActionRisk.LOW
    allowed_agents: frozenset[str] = field(default_factory=frozenset)
    measure: MeasureFn = measure_shape
    transform: TransformFn = identity_transform
    verify: VerifyFn = reject_unverified

    @property
    def allowed_parameters(self) -> frozenset[str]:
        return frozenset(self.required_parameters) | frozenset(self.optional_parameters)

    def to_descriptor(self) -> ActionDescriptor:
        return ActionDescriptor(
            action_type=self.action_type,
            description=self.description,
            parameters=sorted(self.allowed_parameters),
            required_parameters=list(self.required_parameters),
            output_fields=list(self.output_fields),
            risk=self.risk,
            allowed_agents=sorted(self.allowed_agents),
        )


def default_specs() -> list[ActionSpec]:
    remediator = frozenset({REMEDIATOR_ID})
    return [
        ActionSpec(
            action_type=ACTION_REMOVE_DUPLICATES,
            description="Drop exact duplicate rows from the working copy, keeping the first occurrence.",
            required_parameters=(),
            optional_parameters=(),
            output_fields=("rows_before", "rows_after", "duplicates_removed"),
            risk=ActionRisk.LOW,
            allowed_agents=remediator,
            measure=measure_duplicates,
            transform=transform_remove_duplicates,
            verify=verify_remove_duplicates,
        ),
        ActionSpec(
            action_type=ACTION_FILL_MISSING_VALUES,
            description=(
                "Fill missing values in one named working-copy column. "
                "Numeric columns use the median; other columns use UNKNOWN."
            ),
            required_parameters=("column_name",),
            optional_parameters=("strategy",),
            output_fields=("rows_before", "rows_after", "column_name", "filled_count"),
            risk=ActionRisk.MEDIUM,
            allowed_agents=remediator,
            measure=measure_missing,
            transform=transform_fill_missing,
            verify=verify_fill_missing,
        ),
    ]


class ActionRegistry:
    """Explicit allowlist. Model output cannot register or invoke unknown actions."""

    def __init__(self, specs: list[ActionSpec] | None = None) -> None:
        self._specs = {item.action_type: item for item in (specs if specs is not None else default_specs())}

    def all(self) -> list[ActionSpec]:
        return list(self._specs.values())

    def get(self, action_type: str) -> ActionSpec:
        if action_type not in self._specs:
            raise UnknownActionError(f"Action '{action_type}' is not registered")
        return self._specs[action_type]

    def authorize(self, action_type: str, agent_id: str) -> ActionSpec:
        spec = self.get(action_type)
        if agent_id not in spec.allowed_agents:
            raise ActionAuthorizationError(
                f"Agent '{agent_id}' is not authorized to execute '{action_type}'"
            )
        return spec

    def validate_parameters(self, action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(action_type)
        incoming = parameters or {}
        extra = set(incoming) - spec.allowed_parameters
        if extra:
            raise ActionValidationError(
                f"Action '{action_type}' rejected unknown parameters: {sorted(extra)}"
            )
        missing = [name for name in spec.required_parameters if name not in incoming]
        if missing:
            raise ActionValidationError(
                f"Action '{action_type}' is missing required parameters: {missing}"
            )
        return {key: incoming[key] for key in spec.allowed_parameters if key in incoming}


def default_action_registry() -> ActionRegistry:
    return ActionRegistry()


def make_idempotency_key(
    *,
    mission_id: str,
    action_type: str,
    parameters: dict[str, Any],
    input_version: int,
) -> str:
    payload = json.dumps(
        {
            "mission_id": mission_id,
            "action_type": action_type,
            "parameters": parameters or {},
            "input_version": input_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
