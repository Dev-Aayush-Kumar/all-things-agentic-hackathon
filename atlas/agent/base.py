"""Mission planner interface."""

from typing import Protocol

from atlas.domain.models import ExecutionPlan


class MissionPlanner(Protocol):
    """Transforms a natural-language goal into a structured execution plan."""

    @property
    def source_name(self) -> str:
        """Human-readable planner source identifier."""
        ...

    async def create_plan(self, goal: str) -> ExecutionPlan:
        """Generate an execution plan for the given goal."""
        ...
