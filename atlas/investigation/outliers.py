"""IQR-based numeric outlier detection."""

import pandas as pd

from atlas.domain.enums import FindingCategory, Severity
from atlas.domain.models import Finding
from atlas.investigation.findings import new_finding
from atlas.investigation.typing import infer_column_type, numeric_conversion

MIN_VALUES = 8
IQR_MULTIPLIER = 1.5


def analyze_outliers(frame: pd.DataFrame) -> list[Finding]:
    """Detect numeric outliers using Tukey's IQR rule where appropriate."""
    total_rows = int(len(frame))
    findings: list[Finding] = []

    for name in frame.columns:
        if infer_column_type(frame[name]) != "numeric":
            continue
        converted = numeric_conversion(frame[name]).dropna()
        if not _is_appropriate_for_iqr(converted):
            continue

        q1 = float(converted.quantile(0.25))
        q3 = float(converted.quantile(0.75))
        iqr = q3 - q1
        if iqr == 0:
            continue

        lower = q1 - IQR_MULTIPLIER * iqr
        upper = q3 + IQR_MULTIPLIER * iqr
        outlier_mask = (converted < lower) | (converted > upper)
        outlier_count = int(outlier_mask.sum())
        if outlier_count == 0:
            continue

        samples = [float(value) for value in converted[outlier_mask].head(5).tolist()]
        percent = outlier_count / total_rows * 100.0 if total_rows else 0.0
        severity = Severity.HIGH if percent >= 10 else Severity.MEDIUM
        if percent < 2:
            severity = Severity.LOW

        findings.append(
            new_finding(
                category=FindingCategory.NUMERIC_OUTLIER,
                title=f"Numeric outliers in '{name}'",
                description=(
                    f"Column '{name}' has {outlier_count} value(s) outside the IQR "
                    f"fences [{lower:.4g}, {upper:.4g}]."
                ),
                affected_columns=[name],
                evidence={
                    "method": "iqr",
                    "multiplier": IQR_MULTIPLIER,
                    "q1": q1,
                    "q3": q3,
                    "iqr": iqr,
                    "lower_fence": lower,
                    "upper_fence": upper,
                    "outlier_count": outlier_count,
                    "sample_outliers": samples,
                    "numeric_value_count": int(len(converted)),
                },
                affected_row_count=outlier_count,
                total_rows=total_rows,
                severity=severity,
                suggested_action=(
                    f"Review outlier values in '{name}' for data-entry errors or "
                    "genuine extremes before aggregating this column."
                ),
                detection_method="iqr_1.5",
            )
        )
    return findings


def _is_appropriate_for_iqr(values: pd.Series) -> bool:
    if len(values) < MIN_VALUES:
        return False
    unique = values.nunique()
    if unique <= 2:
        return False
    # Skip identifier-like columns: almost all unique integers.
    if unique / len(values) >= 0.95 and (values.dropna() % 1 == 0).all():
        return False
    return True
