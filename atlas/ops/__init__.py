"""Autonomous operations layer: supervisor, registry, specialists."""

from atlas.ops.delegation import LocalDelegationManager
from atlas.ops.registry import AgentRegistry, default_registry
from atlas.ops.supervisor import Supervisor

__all__ = [
    "AgentRegistry",
    "LocalDelegationManager",
    "Supervisor",
    "default_registry",
]
