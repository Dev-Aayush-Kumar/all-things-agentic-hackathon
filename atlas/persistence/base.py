"""Mission repository interface."""

from datetime import datetime
from typing import Protocol

from atlas.domain.models import Mission
from atlas.execution.context import ExecutionContext


class MissionRepository(Protocol):
    """Abstract persistence for missions and durable execution metadata."""

    async def create(self, mission: Mission) -> Mission:
        """Persist a new mission."""
        ...

    async def create_idempotent(
        self,
        mission: Mission,
        idempotency_key: str,
        payload_fingerprint: str,
    ) -> tuple[Mission, bool]:
        """Create a mission or return the existing one for the same key.

        Returns (mission, replayed).
        """
        ...

    async def get(self, mission_id: str) -> Mission | None:
        """Retrieve a mission by ID."""
        ...

    async def update(self, mission: Mission) -> Mission:
        """Unconditional update (system/recovery paths)."""
        ...

    async def update_owned(
        self,
        mission: Mission,
        context: ExecutionContext,
    ) -> Mission:
        """Update only if this worker still holds a valid lease."""
        ...

    async def claim(
        self,
        mission_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> Mission | None:
        """Atomically claim a queued or abandoned mission. None if not claimable."""
        ...

    async def renew_lease(
        self,
        mission_id: str,
        context: ExecutionContext,
        *,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """Extend the lease for the owning worker."""
        ...

    async def list_recoverable(self, now: datetime | None = None) -> list[Mission]:
        """Missions whose leases have expired and are not terminal."""
        ...

    async def requeue_expired(self, mission_id: str, now: datetime | None = None) -> Mission | None:
        """Clear an expired lease and return the mission to QUEUED."""
        ...

    async def exhaust_expired(self, mission_id: str, now: datetime | None = None) -> Mission | None:
        """Mark an expired mission FAILED after max attempts."""
        ...

    async def delete(self, mission_id: str) -> None:
        """Delete a mission (used in tests)."""
        ...
