"""Finding construction helpers."""

from uuid import uuid4

from atlas.domain.enums import FindingCategory, Severity
from atlas.domain.models import Finding


def new_finding(
    *,
    category: FindingCategory,
    title: str,
    description: str,
    affected_columns: list[str],
    evidence: dict,
    affected_row_count: int | None,
    total_rows: int,
    severity: Severity,
    suggested_action: str,
    detection_method: str,
) -> Finding:
    percent = None
    if affected_row_count is not None and total_rows > 0:
        percent = round((affected_row_count / total_rows) * 100.0, 2)
    return Finding(
        finding_id=str(uuid4()),
        category=category,
        title=title,
        description=description,
        affected_columns=affected_columns,
        evidence=evidence,
        affected_row_count=affected_row_count,
        affected_row_percent=percent,
        severity=severity,
        suggested_action=suggested_action,
        detection_method=detection_method,
    )


def severity_from_percent(percent: float, *, material_threshold: float = 20.0) -> Severity:
    """Map an affected-record percentage to a severity band."""
    if percent >= 50:
        return Severity.CRITICAL
    if percent >= material_threshold:
        return Severity.HIGH
    if percent >= 5:
        return Severity.MEDIUM
    return Severity.LOW
