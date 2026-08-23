"""Dataset remediations that operate only on an in-memory working copy."""

from __future__ import annotations

from typing import Any

import pandas as pd

from atlas.domain.exceptions import ActionValidationError
from atlas.domain.models import ActionVerification


def measure_shape(frame: pd.DataFrame, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "row_count": int(len(frame)),
        "column_count": int(frame.shape[1]),
    }


def identity_transform(frame: pd.DataFrame, parameters: dict[str, Any]) -> pd.DataFrame:
    return frame.copy()


def reject_unverified(
    before: dict[str, Any],
    after: pd.DataFrame,
    parameters: dict[str, Any],
) -> ActionVerification:
    return ActionVerification(
        passed=False,
        before=before,
        after=measure_shape(after),
        expected={"verified": True},
        actual={"verified": False},
        summary="No verifier is registered for this action",
    )


def measure_duplicates(frame: pd.DataFrame, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    total = int(len(frame))
    duplicate_count = int(frame.duplicated(keep="first").sum()) if total else 0
    return {
        "row_count": total,
        "column_count": int(frame.shape[1]),
        "duplicate_count": duplicate_count,
        "unique_row_count": total - duplicate_count,
    }


def transform_remove_duplicates(frame: pd.DataFrame, parameters: dict[str, Any]) -> pd.DataFrame:
    return frame.drop_duplicates(keep="first").reset_index(drop=True)


def verify_remove_duplicates(
    before: dict[str, Any],
    after: pd.DataFrame,
    parameters: dict[str, Any],
) -> ActionVerification:
    after_state = measure_duplicates(after)
    expected = {
        "duplicate_count": 0,
        "row_count": before.get("unique_row_count"),
    }
    actual = {
        "duplicate_count": after_state["duplicate_count"],
        "row_count": after_state["row_count"],
    }
    passed = (
        after_state["duplicate_count"] == 0
        and after_state["row_count"] == before.get("unique_row_count")
    )
    removed = int(before.get("duplicate_count") or 0)
    return ActionVerification(
        passed=passed,
        before=before,
        after=after_state,
        expected=expected,
        actual=actual,
        summary=(
            f"Removed {removed} duplicate row(s); working copy now has "
            f"{after_state['row_count']} unique row(s)."
            if passed
            else "Duplicate-row postcondition failed after REMOVE_DUPLICATES."
        ),
    )


def measure_missing(frame: pd.DataFrame, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    parameters = parameters or {}
    column = parameters.get("column_name")
    total = int(len(frame))
    missing = None
    if isinstance(column, str) and column in frame.columns:
        missing = int(frame[column].isna().sum())
    return {
        "row_count": total,
        "column_count": int(frame.shape[1]),
        "column_name": column,
        "missing_count": missing,
    }


def transform_fill_missing(frame: pd.DataFrame, parameters: dict[str, Any]) -> pd.DataFrame:
    column = parameters.get("column_name")
    if not isinstance(column, str) or column not in frame.columns:
        raise ActionValidationError(
            f"Column '{column}' is not present in the working copy"
        )
    strategy = str(parameters.get("strategy") or "auto")
    if strategy not in {"auto", "median", "constant"}:
        raise ActionValidationError(
            f"Unsupported fill strategy '{strategy}'. Allowed: auto, median, constant"
        )
    result = frame.copy()
    series = result[column]
    if strategy == "constant":
        result[column] = series.fillna("UNKNOWN")
        return result
    numeric = pd.to_numeric(series, errors="coerce")
    use_median = strategy == "median" or (
        strategy == "auto" and _should_fill_numeric(series, numeric)
    )
    if use_median:
        fill_value = numeric.median()
        if pd.isna(fill_value):
            raise ActionValidationError(
                f"Cannot compute a median fill for '{column}' because it has no numeric values"
            )
        result[column] = numeric.fillna(fill_value)
        return result
    result[column] = series.fillna("UNKNOWN")
    return result


def verify_fill_missing(
    before: dict[str, Any],
    after: pd.DataFrame,
    parameters: dict[str, Any],
) -> ActionVerification:
    after_state = measure_missing(after, parameters)
    expected = {
        "missing_count": 0,
        "row_count": before.get("row_count"),
    }
    actual = {
        "missing_count": after_state["missing_count"],
        "row_count": after_state["row_count"],
    }
    passed = (
        after_state["missing_count"] == 0
        and after_state["row_count"] == before.get("row_count")
    )
    filled = int((before.get("missing_count") or 0))
    return ActionVerification(
        passed=passed,
        before=before,
        after=after_state,
        expected=expected,
        actual=actual,
        summary=(
            f"Filled {filled} missing value(s) in '{parameters.get('column_name')}'."
            if passed
            else f"Missing-value postcondition failed for '{parameters.get('column_name')}'."
        ),
    )


def _should_fill_numeric(original: pd.Series, numeric: pd.Series) -> bool:
    if pd.api.types.is_numeric_dtype(original):
        return True
    non_null = original.notna().sum()
    if non_null == 0:
        return False
    coerced = numeric.notna().sum()
    return coerced == non_null
