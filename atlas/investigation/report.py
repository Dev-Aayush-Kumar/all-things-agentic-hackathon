"""Build a structured investigation report from measured findings."""

from atlas.domain.enums import PlannerSource
from atlas.domain.models import (
    DatasetSummary,
    Finding,
    InvestigationReport,
    RecommendedAction,
)
from atlas.investigation.pipeline import InvestigationResult


def build_report(
    *,
    dataset_id: str | None,
    original_filename: str | None,
    result: InvestigationResult,
    mission_summary: str,
    investigation_summary: str,
    overall_assessment: str,
    recommended_actions: list[RecommendedAction],
    reasoning_source: PlannerSource,
) -> InvestigationReport:
    """Compose the final report. Findings remain the measured evidence."""
    return InvestigationReport(
        mission_summary=mission_summary,
        dataset_summary=DatasetSummary(
            dataset_id=dataset_id,
            original_filename=original_filename,
            row_count=result.profile.row_count,
            column_count=result.profile.column_count,
            columns=result.profile.columns,
        ),
        investigation_summary=investigation_summary,
        findings=result.findings,
        recommended_actions=recommended_actions,
        overall_assessment=overall_assessment,
        reasoning_source=reasoning_source,
    )


def default_actions_from_findings(findings: list[Finding]) -> list[RecommendedAction]:
    """Turn each finding's suggested action into a structured recommendation."""
    actions: list[RecommendedAction] = []
    for finding in findings:
        actions.append(
            RecommendedAction(
                action_id=f"action_{finding.priority}",
                title=finding.title,
                description=finding.suggested_action,
                related_finding_ids=[finding.finding_id],
                priority=finding.priority,
            )
        )
    return actions
