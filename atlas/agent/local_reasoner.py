"""Local development fallback for investigation reasoning.

This module does NOT use Gemini or Google ADK. Summaries and recommended
actions are derived only from measured findings and the dataset profile.
"""

from collections import Counter

from atlas.agent.reasoner_base import InvestigationReasoner, ReasoningResult
from atlas.domain.enums import PlannerSource, Severity
from atlas.domain.models import DatasetProfile, Finding
from atlas.investigation.report import default_actions_from_findings


class LocalFallbackReasoner(InvestigationReasoner):
    """Template-based interpretation of measured investigation findings."""

    @property
    def source_name(self) -> str:
        return "LOCAL_DEVELOPMENT_FALLBACK"

    async def interpret(
        self,
        goal: str,
        profile: DatasetProfile,
        findings: list[Finding],
    ) -> ReasoningResult:
        counts = Counter(finding.category.value for finding in findings)
        high_or_worse = [
            finding
            for finding in findings
            if finding.severity in {Severity.HIGH, Severity.CRITICAL}
        ]
        top = findings[:3]

        mission_summary = (
            f"ATLAS investigated the uploaded dataset against the goal: {goal} "
            f"The dataset has {profile.row_count} rows and {profile.column_count} columns."
        )
        if findings:
            category_text = ", ".join(
                f"{name}={count}" for name, count in sorted(counts.items())
            )
            investigation_summary = (
                f"{len(findings)} evidence-based finding(s) were produced "
                f"({category_text}). "
                f"{len(high_or_worse)} finding(s) are HIGH or CRITICAL severity."
            )
        else:
            investigation_summary = (
                "No data-quality findings were produced from the implemented "
                "checks on this dataset."
            )

        if not findings:
            overall = (
                "The implemented investigation checks did not detect missing values, "
                "duplicates, type/format issues, outliers, or explicit consistency "
                "violations. This is not a guarantee that the data is issue-free."
            )
        elif high_or_worse:
            titles = "; ".join(item.title for item in high_or_worse[:3])
            overall = (
                "The dataset has material quality issues that should be resolved "
                f"before relying on it for analysis. Highest-impact items: {titles}."
            )
        else:
            titles = "; ".join(item.title for item in top) if top else "none"
            overall = (
                "Issues were detected but none reached HIGH/CRITICAL severity. "
                f"Address the leading items first: {titles}."
            )

        return ReasoningResult(
            mission_summary=mission_summary,
            investigation_summary=investigation_summary,
            overall_assessment=overall,
            recommended_actions=default_actions_from_findings(findings),
            source=PlannerSource.LOCAL_FALLBACK,
        )
