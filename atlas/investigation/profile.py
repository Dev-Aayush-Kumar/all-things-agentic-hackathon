"""Deterministic dataset profiling."""

import pandas as pd

from atlas.domain.models import ColumnProfile, DatasetProfile, NumericStats
from atlas.investigation.typing import infer_column_type, non_null_series, numeric_conversion


def build_profile(frame: pd.DataFrame) -> DatasetProfile:
    """Measure dataset shape, inferred types, and numeric statistics."""
    row_count = int(len(frame))
    columns: list[ColumnProfile] = []

    for name in frame.columns:
        series = frame[name]
        null_count = int(series.isna().sum())
        non_null_count = row_count - null_count
        inferred = infer_column_type(series)
        unique_count = int(non_null_series(series).nunique())
        numeric_stats = None
        if inferred == "numeric":
            numeric_stats = _numeric_stats(series)

        columns.append(
            ColumnProfile(
                name=str(name),
                inferred_type=inferred,
                non_null_count=non_null_count,
                null_count=null_count,
                null_percent=_percent(null_count, row_count),
                unique_count=unique_count,
                numeric_stats=numeric_stats,
            )
        )

    return DatasetProfile(
        row_count=row_count,
        column_count=int(frame.shape[1]),
        columns=columns,
    )


def _numeric_stats(series: pd.Series) -> NumericStats | None:
    converted = numeric_conversion(series).dropna()
    if converted.empty:
        return None
    return NumericStats(
        min=float(converted.min()),
        max=float(converted.max()),
        mean=float(converted.mean()),
        median=float(converted.median()),
        std=float(converted.std(ddof=0)) if len(converted) > 1 else 0.0,
    )


def _percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round((part / whole) * 100.0, 2)
