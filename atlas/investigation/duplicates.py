"""Duplicate-row analysis."""

import pandas as pd

from atlas.domain.enums import FindingCategory
from atlas.domain.models import Finding
from atlas.investigation.findings import new_finding, severity_from_percent


def analyze_duplicates(frame: pd.DataFrame) -> list[Finding]:
    """Count exact duplicate rows in the source frame."""
    total_rows = int(len(frame))
    if total_rows == 0:
        return []

    duplicate_mask = frame.duplicated(keep="first")
    duplicate_count = int(duplicate_mask.sum())
    if duplicate_count == 0:
        return []

    percent = duplicate_count / total_rows * 100.0
    return [
        new_finding(
            category=FindingCategory.DUPLICATE_ROWS,
            title="Duplicate rows detected",
            description=(
                f"{duplicate_count} row(s) are exact duplicates of earlier rows "
                f"({percent:.2f}% of {total_rows} rows)."
            ),
            affected_columns=list(frame.columns.astype(str)),
            evidence={
                "duplicate_row_count": duplicate_count,
                "total_rows": total_rows,
                "duplicate_percent": round(percent, 2),
                "unique_row_count": total_rows - duplicate_count,
            },
            affected_row_count=duplicate_count,
            total_rows=total_rows,
            severity=severity_from_percent(percent, material_threshold=10.0),
            suggested_action=(
                "Deduplicate the dataset after confirming whether the repeated "
                "rows are true copies or distinct records that lack a unique key."
            ),
            detection_method="exact_row_duplication",
        )
    ]
