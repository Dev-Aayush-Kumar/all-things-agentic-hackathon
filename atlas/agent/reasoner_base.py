"""Investigation reasoning interface.

Deterministic investigation produces findings. A reasoner interprets those
findings against the user's goal. It must not invent unsupported findings.
"""

from typing import Protocol

from atlas.domain.models import DatasetProfile, Finding, RecommendedAction
from atlas.domain.enums import PlannerSource


class ReasoningResult:
    """Interpretation produced from measured findings."""

    def __init__(
        self,
        mission_summary: str,
        investigation_summary: str,
        overall_assessment: str,
        recommended_actions: list[RecommendedAction],
        source: PlannerSource,
    ) -> None:
        self.mission_summary = mission_summary
        self.investigation_summary = investigation_summary
        self.overall_assessment = overall_assessment
        self.recommended_actions = recommended_actions
        self.source = source


class InvestigationReasoner(Protocol):
    """Interprets investigation findings in the context of a mission goal."""

    @property
    def source_name(self) -> str:
        ...

    async def interpret(
        self,
        goal: str,
        profile: DatasetProfile,
        findings: list[Finding],
    ) -> ReasoningResult:
        """Reason about measured findings. Do not fabricate new findings."""
        ...
