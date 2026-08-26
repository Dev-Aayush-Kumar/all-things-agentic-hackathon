"""Agent and planning layer.

This package init is intentionally lazy. Importing ``atlas.agent.gemini``
must not load factory/ADK modules, because Settings imports those helpers.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "InvestigationReasoner",
    "MissionPlanner",
    "create_investigation_reasoner",
    "create_mission_planner",
    "resolve_initial_tools",
]


def __getattr__(name: str) -> Any:
    if name == "MissionPlanner":
        from atlas.agent.base import MissionPlanner

        return MissionPlanner
    if name == "InvestigationReasoner":
        from atlas.agent.reasoner_base import InvestigationReasoner

        return InvestigationReasoner
    if name in {
        "create_investigation_reasoner",
        "create_mission_planner",
        "resolve_initial_tools",
    }:
        from atlas.agent.factory import (
            create_investigation_reasoner,
            create_mission_planner,
            resolve_initial_tools,
        )

        exports = {
            "create_investigation_reasoner": create_investigation_reasoner,
            "create_mission_planner": create_mission_planner,
            "resolve_initial_tools": resolve_initial_tools,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
