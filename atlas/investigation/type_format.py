"""Type and format anomaly analysis."""

import pandas as pd

from atlas.domain.enums import FindingCategory, Severity
from atlas.domain.models import Finding
from atlas.investigation.findings import new_finding, severity_from_percent
from atlas.investigation.typing import (
    DATETIME_SUCCESS_THRESHOLD,
    NUMERIC_SUCCESS_THRESHOLD,
    conversion_success_rate,
    datetime_conversion,
    infer_column_type,
    non_null_series,
    numeric_conversion,
)

CATEGORICAL_UNIQUE_CAP = 50


def analyze_type_format(frame: pd.DataFrame) -> list[Finding]:
    """Detect coercion failures and categorical formatting inconsistencies."""
    total_rows = int(len(frame))
    findings: list[Finding] = []

    for name in frame.columns:
        series = frame[name]
        values = non_null_series(series)
        if values.empty:
            continue
        inferred = infer_column_type(series)

        if inferred == "numeric":
            findings.extend(_numeric_coercion_findings(name, series, values, total_rows))
        elif inferred == "datetime":
            findings.extend(_datetime_coercion_findings(name, series, values, total_rows))
        else:
            findings.extend(_categorical_format_findings(name, values, total_rows))

    return findings


def _numeric_coercion_findings(
    name: str,
    series: pd.Series,
    values: pd.Series,
    total_rows: int,
) -> list[Finding]:
    converted = numeric_conversion(series)
    invalid_count = int(converted.isna().sum())
    success = conversion_success_rate(converted, len(values))
    if invalid_count == 0 or success < NUMERIC_SUCCESS_THRESHOLD:
        return []

    samples = values[converted.isna()].head(5).tolist()
    percent = invalid_count / total_rows * 100.0 if total_rows else 0.0
    return [
        new_finding(
            category=FindingCategory.TYPE_FORMAT_ANOMALY,
            title=f"Non-numeric values in numeric column '{name}'",
            description=(
                f"Column '{name}' is predominantly numeric, but {invalid_count} "
                f"value(s) cannot be coerced to a number."
            ),
            affected_columns=[name],
            evidence={
                "invalid_count": invalid_count,
                "non_null_count": int(len(values)),
                "coercion_success_rate": round(success, 4),
                "sample_invalid_values": samples,
            },
            affected_row_count=invalid_count,
            total_rows=total_rows,
            severity=severity_from_percent(percent),
            suggested_action=(
                f"Standardize or repair non-numeric values in '{name}' "
                "(examples: " + ", ".join(repr(s) for s in samples) + ")."
            ),
            detection_method="numeric_coercion",
        )
    ]


def _datetime_coercion_findings(
    name: str,
    series: pd.Series,
    values: pd.Series,
    total_rows: int,
) -> list[Finding]:
    converted = datetime_conversion(series)
    invalid_count = int(converted.isna().sum())
    success = conversion_success_rate(converted, len(values))
    if invalid_count == 0 or success < DATETIME_SUCCESS_THRESHOLD:
        return []

    samples = values[converted.isna()].head(5).tolist()
    percent = invalid_count / total_rows * 100.0 if total_rows else 0.0
    return [
        new_finding(
            category=FindingCategory.TYPE_FORMAT_ANOMALY,
            title=f"Invalid date values in '{name}'",
            description=(
                f"Column '{name}' is predominantly datetime, but {invalid_count} "
                f"value(s) cannot be parsed as dates."
            ),
            affected_columns=[name],
            evidence={
                "invalid_count": invalid_count,
                "non_null_count": int(len(values)),
                "coercion_success_rate": round(success, 4),
                "sample_invalid_values": samples,
            },
            affected_row_count=invalid_count,
            total_rows=total_rows,
            severity=severity_from_percent(percent),
            suggested_action=(
                f"Normalize date formats in '{name}' and correct unparseable values."
            ),
            detection_method="datetime_coercion",
        )
    ]


def _categorical_format_findings(
    name: str,
    values: pd.Series,
    total_rows: int,
) -> list[Finding]:
    unique_raw = int(values.nunique())
    if unique_raw < 2 or unique_raw > CATEGORICAL_UNIQUE_CAP:
        return []

    normalized = values.str.strip().str.lower()
    unique_normalized = int(normalized.nunique())
    collapsed = unique_raw - unique_normalized
    if collapsed <= 0:
        return []

    groups: dict[str, list[str]] = {}
    for raw in sorted(values.unique().tolist()):
        key = raw.strip().lower()
        groups.setdefault(key, [])
        if raw not in groups[key]:
            groups[key].append(raw)
    variants = {key: variants for key, variants in groups.items() if len(variants) > 1}
    if not variants:
        return []

    affected = int((normalized.map(lambda value: len(groups.get(value, [])) > 1)).sum())
    sample = {key: items for key, items in list(variants.items())[:5]}
    return [
        new_finding(
            category=FindingCategory.CATEGORICAL_INCONSISTENCY,
            title=f"Inconsistent categorical formatting in '{name}'",
            description=(
                f"Column '{name}' has {unique_raw} distinct raw values that collapse "
                f"to {unique_normalized} after trimming whitespace and ignoring case."
            ),
            affected_columns=[name],
            evidence={
                "raw_unique_count": unique_raw,
                "normalized_unique_count": unique_normalized,
                "collapsed_variant_groups": collapsed,
                "sample_variants": sample,
            },
            affected_row_count=affected,
            total_rows=total_rows,
            severity=Severity.MEDIUM if affected / max(total_rows, 1) >= 0.1 else Severity.LOW,
            suggested_action=(
                f"Normalize casing and whitespace in '{name}' to a single canonical "
                "value per category."
            ),
            detection_method="categorical_normalization",
        )
    ]
