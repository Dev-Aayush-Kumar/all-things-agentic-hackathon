"""Controlled autonomous action layer.

Observation tools gather evidence. Action tools change a sandbox working copy.
Gemini may propose an action; ATLAS authorizes, executes, and verifies it.
"""

from atlas.ops.actions.executor import ActionContext, ActionExecutor
from atlas.ops.actions.policy import goal_requests_remediation, propose_action_follow_ups
from atlas.ops.actions.registry import (
    ACTION_CAPABILITIES,
    ACTION_FILL_MISSING_VALUES,
    ACTION_REMOVE_DUPLICATES,
    CAPABILITY_FILL_MISSING,
    CAPABILITY_REMOVE_DUPLICATES,
    ActionRegistry,
    ActionSpec,
    default_action_registry,
    make_idempotency_key,
)

__all__ = [
    "ACTION_CAPABILITIES",
    "ACTION_FILL_MISSING_VALUES",
    "ACTION_REMOVE_DUPLICATES",
    "ActionContext",
    "ActionExecutor",
    "ActionRegistry",
    "ActionSpec",
    "CAPABILITY_FILL_MISSING",
    "CAPABILITY_REMOVE_DUPLICATES",
    "default_action_registry",
    "goal_requests_remediation",
    "make_idempotency_key",
    "propose_action_follow_ups",
]
