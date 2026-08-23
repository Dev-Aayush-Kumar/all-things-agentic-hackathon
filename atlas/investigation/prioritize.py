"""Deterministic finding prioritization.

Priority is an integer rank starting at 1 (highest). Ranking is derived only
from measured evidence, never randomly.

Score for each finding:

    score = affected_percent * category_weight + severity_points

where:
- affected_percent is the share of rows implicated (0 if unknown)
- category_weight reflects how much the issue blocks reliable analysis
- severity_points encodes the already-derived severity band

Ties are broken by category name, then title, so results are stable.
"""

from atlas.domain.enums import FindingCategory, Severity
from atlas.domain.models import Finding

SEVERITY_POINTS = {
    Severity.CRITICAL: 40,
    Severity.HIGH: 25,
    Severity.MEDIUM: 12,
    Severity.LOW: 4,
}

CATEGORY_WEIGHT = {
    FindingCategory.MISSING_DATA: 1.2,
    FindingCategory.DUPLICATE_ROWS: 1.1,
    FindingCategory.TYPE_FORMAT_ANOMALY: 1.4,
    FindingCategory.CONSISTENCY_VIOLATION: 1.3,
    FindingCategory.NUMERIC_OUTLIER: 0.9,
    FindingCategory.CATEGORICAL_INCONSISTENCY: 0.7,
}


def prioritize_findings(findings: list[Finding]) -> list[Finding]:
    """Return findings sorted by descending priority score with ranks assigned."""
    scored = sorted(
        findings,
        key=lambda finding: (-_score(finding), finding.category.value, finding.title),
    )
    ranked: list[Finding] = []
    for index, finding in enumerate(scored, start=1):
        ranked.append(finding.model_copy(update={"priority": index}))
    return ranked


def _score(finding: Finding) -> float:
    affected = finding.affected_row_percent or 0.0
    weight = CATEGORY_WEIGHT.get(finding.category, 1.0)
    return affected * weight + SEVERITY_POINTS.get(finding.severity, 0)
