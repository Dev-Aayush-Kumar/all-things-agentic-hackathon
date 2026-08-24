"""Experience and strategy repository interfaces."""

from typing import Protocol

from atlas.domain.models import ExperienceRecord, StrategyRecord


class ExperienceRepository(Protocol):
    """Abstract persistence for evaluated mission experiences."""

    async def upsert(self, record: ExperienceRecord) -> ExperienceRecord:
        """Insert or replace by fingerprint (one experience per mission)."""
        ...

    async def get(self, experience_id: str) -> ExperienceRecord | None:
        ...

    async def get_by_mission(self, mission_id: str) -> ExperienceRecord | None:
        ...

    async def find_by_fingerprint(self, fingerprint: str) -> ExperienceRecord | None:
        ...


class StrategyRepository(Protocol):
    """Abstract persistence for aggregated strategies."""

    async def upsert(self, record: StrategyRecord) -> StrategyRecord:
        """Insert or replace by fingerprint."""
        ...

    async def get(self, strategy_id: str) -> StrategyRecord | None:
        ...

    async def find_by_fingerprint(self, fingerprint: str) -> StrategyRecord | None:
        ...

    async def list_candidates(self, *, limit: int = 100) -> list[StrategyRecord]:
        ...

    async def list_public(self, *, limit: int = 50) -> list[StrategyRecord]:
        ...
