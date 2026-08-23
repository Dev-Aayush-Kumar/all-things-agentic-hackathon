"""Build a structured investigation report from measured findings."""

from atlas.domain.enums import PlannerSource
from atlas.domain.enums import EvidenceSourceType
from atlas.domain.models import (
    ActionSummary,
    DatasetSummary,
    EvidenceRecord,
    ExternalReference,
    Finding,
    InvestigationReport,
    RecommendedAction,
    WorkingCopyPublic,
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
    actions_performed: list[ActionSummary] | None = None,
    working_copy: WorkingCopyPublic | None = None,
    remaining_issues: list[str] | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
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
        actions_performed=list(actions_performed or []),
        working_copy=working_copy,
        remaining_issues=list(remaining_issues or []),
        external_references=_external_references(evidence_records or []),
    )


def _external_references(records: list[EvidenceRecord]) -> list[ExternalReference]:
    references: list[ExternalReference] = []
    for item in records:
        if item.source_type != EvidenceSourceType.EXTERNAL:
            continue
        if item.execution_status != "SUCCEEDED":
            continue
        facts = item.observed_facts
        url = item.source_url or facts.get("source_url")
        if not isinstance(url, str):
            continue
        excerpt = facts.get("excerpt")
        references.append(
            ExternalReference(
                tool_name=item.tool_name,
                source_url=url,
                title=facts.get("title") if isinstance(facts.get("title"), str) else None,
                excerpt=excerpt if isinstance(excerpt, str) else "",
                status_code=facts.get("status_code") if isinstance(facts.get("status_code"), int) else None,
                retrieved_at=item.created_at,
                evidence_id=item.evidence_id,
            )
        )
    return references


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
