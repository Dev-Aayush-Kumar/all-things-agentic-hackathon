"""Missing-value analysis."""

import pandas as pd

from atlas.domain.enums import FindingCategory
from atlas.domain.models import Finding
from atlas.investigation.findings import new_finding, severity_from_percent

MATERIAL_MISSING_PERCENT = 20.0


def analyze_missing(frame: pd.DataFrame) -> list[Finding]:
    """Measure missing values per column from the actual frame."""
    total_rows = int(len(frame))
    findings: list[Finding] = []

    for name in frame.columns:
        missing = int(frame[name].isna().sum())
        if missing == 0:
            continue
        percent = (missing / total_rows * 100.0) if total_rows else 0.0
        material = percent >= MATERIAL_MISSING_PERCENT
        severity = severity_from_percent(percent)
        findings.append(
            new_finding(
                category=FindingCategory.MISSING_DATA,
                title=f"Missing values in '{name}'",
                description=(
                    f"Column '{name}' has {missing} missing value(s) "
                    f"({percent:.2f}% of {total_rows} rows)"
                    + (
                        " and is materially incomplete."
                        if material
                        else "."
                    )
                ),
                affected_columns=[str(name)],
                evidence={
                    "missing_count": missing,
                    "total_rows": total_rows,
                    "missing_percent": round(percent, 2),
                    "materially_incomplete": material,
                    "material_threshold_percent": MATERIAL_MISSING_PERCENT,
                },
                affected_row_count=missing,
                total_rows=total_rows,
                severity=severity,
                suggested_action=(
                    f"Investigate why '{name}' is incomplete and impute, collect, "
                    "or exclude the missing records before downstream analysis."
                ),
                detection_method="null_count",
            )
        )
    return findings
