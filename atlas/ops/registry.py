"""In-process specialist agent registry."""

from __future__ import annotations

from collections.abc import Callable

from atlas.agent.tools import (
    ANALYZE_CONSISTENCY,
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    ANALYZE_TYPE_FORMAT,
    INSPECT_COLUMN,
    INVESTIGATION_TOOLS,
    PROFILE_DATASET,
)
from atlas.domain.enums import AgentRole
from atlas.domain.models import AgentDescriptor

DATA_ANALYST_ID = "atlas.data_analyst"
INVESTIGATOR_ID = "atlas.investigator"
REPORTER_ID = "atlas.reporter"
SUPERVISOR_ID = "atlas.supervisor"

CAPABILITY_PROFILE = PROFILE_DATASET
CAPABILITY_INVESTIGATE = "investigate_evidence"
CAPABILITY_INVESTIGATE_COLUMN = "investigate_column"
CAPABILITY_SYNTHESIZE = "synthesize_report"

ANALYST_TOOLS = list(INVESTIGATION_TOOLS)
INVESTIGATOR_TOOLS = [INSPECT_COLUMN]
REPORTER_TOOLS: list[str] = []

ANALYST_CAPABILITIES = list(INVESTIGATION_TOOLS)
INVESTIGATOR_CAPABILITIES = [CAPABILITY_INVESTIGATE, CAPABILITY_INVESTIGATE_COLUMN]
REPORTER_CAPABILITIES = [CAPABILITY_SYNTHESIZE, "prioritize_findings"]


class UnknownCapabilityError(LookupError):
    """Raised when no registered agent advertises a capability."""


class UnknownAgentError(LookupError):
    """Raised when an agent id is not in the registry."""


def _default_descriptors() -> list[AgentDescriptor]:
    return [
        AgentDescriptor(
            id=DATA_ANALYST_ID,
            name="Data Analyst",
            role=AgentRole.DATA_ANALYST,
            description=(
                "Profiles datasets and measures quality issues: missingness, "
                "duplicates, type/format, outliers, and consistency."
            ),
            capabilities=list(ANALYST_CAPABILITIES),
            allowed_tools=list(ANALYST_TOOLS),
        ),
        AgentDescriptor(
            id=INVESTIGATOR_ID,
            name="Investigator",
            role=AgentRole.INVESTIGATOR,
            description=(
                "Examines measured evidence, connects related findings, and "
                "decides whether additional investigation is warranted."
            ),
            capabilities=list(INVESTIGATOR_CAPABILITIES),
            allowed_tools=list(INVESTIGATOR_TOOLS),
        ),
        AgentDescriptor(
            id=REPORTER_ID,
            name="Reporter",
            role=AgentRole.REPORTER,
            description=(
                "Synthesizes verified findings, prioritizes them, and produces "
                "the final mission report with evidence separated from interpretation."
            ),
            capabilities=list(REPORTER_CAPABILITIES),
            allowed_tools=list(REPORTER_TOOLS),
        ),
        AgentDescriptor(
            id=SUPERVISOR_ID,
            name="Supervisor",
            role=AgentRole.SUPERVISOR,
            description="Owns the mission, delegates work, observes evidence, and replans.",
            capabilities=["orchestrate_mission"],
            allowed_tools=[],
        ),
    ]


class AgentRegistry:
    """Local in-process registry. Not a distributed service."""

    def __init__(self, descriptors: list[AgentDescriptor] | None = None) -> None:
        self._descriptors = {
            item.id: item for item in (descriptors or _default_descriptors())
        }
        self._factories: dict[str, Callable[[], object]] = {}

    def register(self, descriptor: AgentDescriptor, factory: Callable[[], object] | None = None) -> None:
        self._descriptors[descriptor.id] = descriptor
        if factory is not None:
            self._factories[descriptor.id] = factory

    def all(self) -> list[AgentDescriptor]:
        return [item for item in self._descriptors.values() if item.role != AgentRole.SUPERVISOR]

    def list_all(self) -> list[AgentDescriptor]:
        return list(self._descriptors.values())

    def get(self, agent_id: str) -> AgentDescriptor:
        if agent_id not in self._descriptors:
            raise UnknownAgentError(agent_id)
        return self._descriptors[agent_id]

    def match(self, capability: str) -> AgentDescriptor:
        """Return the first specialist that advertises the capability."""
        for descriptor in self._descriptors.values():
            if descriptor.role == AgentRole.SUPERVISOR:
                continue
            if capability in descriptor.capabilities:
                return descriptor
        raise UnknownCapabilityError(capability)

    def allowed_tools(self, agent_id: str) -> frozenset[str]:
        return frozenset(self.get(agent_id).allowed_tools)

    def authorize_tool(self, agent_id: str, tool_name: str) -> None:
        allowed = self.allowed_tools(agent_id)
        if tool_name not in allowed:
            raise PermissionError(
                f"Agent '{agent_id}' is not authorized to use tool '{tool_name}'"
            )


def default_registry() -> AgentRegistry:
    return AgentRegistry()
