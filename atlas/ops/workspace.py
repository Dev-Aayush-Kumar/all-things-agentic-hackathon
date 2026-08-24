"""Shared workspace passed from the supervisor to specialists."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.agent.tools import ToolContext, ToolResult
from atlas.config.settings import Settings
from atlas.domain.enums import PlannerSource
from atlas.domain.models import DatasetProfile, MemoryRecord, Mission, StrategyRecord
from atlas.ops.registry import AgentRegistry
from atlas.storage.base import DatasetStorage

PersistFn = Callable[[], Awaitable[None]]


@dataclass
class MissionWorkspace:
    """Mutable mission execution context. Specialists must not escape this."""

    mission: Mission
    tool_context: ToolContext
    persist: PersistFn
    lock: asyncio.Lock
    settings: Settings
    reasoner: InvestigationReasoner
    registry: AgentRegistry
    plan_source: PlannerSource
    tool_results: list[ToolResult] = field(default_factory=list)
    inspected_columns: set[str] = field(default_factory=set)
    step_delay_seconds: float = 0.0
    dataset_storage: DatasetStorage | None = None
    retrieved_memories: list[MemoryRecord] = field(default_factory=list)
    retrieved_strategies: list[StrategyRecord] = field(default_factory=list)

    @property
    def profile(self) -> DatasetProfile | None:
        return self.mission.dataset_profile
