"""Decision-maker contract used by the supervisor.

Gemini and the local fallback implement the same typed interface.
Neither implementation may execute tools or actions.
"""

from __future__ import annotations

from typing import Any, Protocol

from atlas.domain.enums import PlannerSource
from atlas.domain.models import ModelDecision


class DecisionMaker(Protocol):
    """Proposes the next typed supervisor decision. Never executes it."""

    @property
    def source(self) -> PlannerSource: ...

    @property
    def drives_initial_plan(self) -> bool: ...

    async def decide(self, context: dict[str, Any]) -> ModelDecision: ...
