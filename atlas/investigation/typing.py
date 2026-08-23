"""Column type inference helpers."""

from __future__ import annotations

import pandas as pd

NUMERIC_SUCCESS_THRESHOLD = 0.8
DATETIME_SUCCESS_THRESHOLD = 0.8
BOOLEAN_VALUES = {"true", "false", "yes", "no", "y", "n", "t", "f", "0", "1"}


def non_null_series(series: pd.Series) -> pd.Series:
    """Return non-null values with surrounding whitespace stripped."""
    cleaned = series.dropna().astype(str).str.strip()
    return cleaned[cleaned != ""]


def numeric_conversion(series: pd.Series) -> pd.Series:
    """Coerce values to numeric; invalid values become NaN."""
    values = non_null_series(series)
    return pd.to_numeric(values, errors="coerce")


def datetime_conversion(series: pd.Series) -> pd.Series:
    """Coerce values to datetime; invalid values become NaT."""
    values = non_null_series(series)
    iso = pd.to_datetime(values, errors="coerce", format="ISO8601")
    if conversion_success_rate(iso, len(values)) >= DATETIME_SUCCESS_THRESHOLD:
        return iso
    return pd.to_datetime(values, errors="coerce", format="%Y-%m-%d")


def conversion_success_rate(converted: pd.Series, original_count: int) -> float:
    if original_count == 0:
        return 0.0
    return float(converted.notna().sum()) / original_count


def infer_column_type(series: pd.Series) -> str:
    """Infer a coarse column type from raw string values."""
    values = non_null_series(series)
    if values.empty:
        return "string"

    unique_normalized = {value.lower() for value in values.unique()}
    if unique_normalized.issubset(BOOLEAN_VALUES) and len(unique_normalized) <= 4:
        return "boolean"

    numeric = numeric_conversion(series)
    if conversion_success_rate(numeric, len(values)) >= NUMERIC_SUCCESS_THRESHOLD:
        return "numeric"

    datetimes = datetime_conversion(series)
    if conversion_success_rate(datetimes, len(values)) >= DATETIME_SUCCESS_THRESHOLD:
        return "datetime"

    return "string"
