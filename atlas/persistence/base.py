"""Mission repository interface."""

from typing import Protocol

from atlas.domain.models import Mission


class MissionRepository(Protocol):
    """Abstract persistence for missions."""

    async def create(self, mission: Mission) -> Mission:
        """Persist a new mission."""
        ...

    async def get(self, mission_id: str) -> Mission | None:
        """Retrieve a mission by ID."""
        ...

    async def update(self, mission: Mission) -> Mission:
        """Update an existing mission."""
        ...

    async def delete(self, mission_id: str) -> None:
        """Delete a mission (used in tests)."""
        ...
