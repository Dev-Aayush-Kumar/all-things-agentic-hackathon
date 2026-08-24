"""Deterministic signatures for goals, datasets, and strategies.

Dataset signatures carry structural traits only. They never include cell values,
file bytes, or original CSV content.
"""

from __future__ import annotations

import hashlib
import re

from atlas.domain.enums import MissingnessBucket, MissionCategory, RowCountBucket
from atlas.domain.models import DatasetCharacteristics, DatasetSignature, Mission

TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

RELATED_CATEGORIES: dict[MissionCategory, frozenset[MissionCategory]] = {
    MissionCategory.DATA_QUALITY: frozenset(
        {
            MissionCategory.DUPLICATES,
            MissionCategory.MISSING,
            MissionCategory.OUTLIERS,
            MissionCategory.CONSISTENCY,
            MissionCategory.GENERAL,
        }
    ),
    MissionCategory.DUPLICATES: frozenset({MissionCategory.DATA_QUALITY}),
    MissionCategory.MISSING: frozenset({MissionCategory.DATA_QUALITY}),
    MissionCategory.OUTLIERS: frozenset({MissionCategory.DATA_QUALITY}),
    MissionCategory.CONSISTENCY: frozenset({MissionCategory.DATA_QUALITY}),
    MissionCategory.GENERAL: frozenset({MissionCategory.DATA_QUALITY}),
}


def classify_goal(goal: str) -> MissionCategory:
    text = (goal or "").lower()
    broad_quality = any(
        word in text
        for word in ("quality", "problem", "investigat", "issue", "anomal", "clean")
    )
    if broad_quality:
        return MissionCategory.DATA_QUALITY
    if any(word in text for word in ("duplicate", "duplicated", "dedup", "repeat row")):
        return MissionCategory.DUPLICATES
    if any(word in text for word in ("outlier", "extreme", "iqr")):
        return MissionCategory.OUTLIERS
    if any(word in text for word in ("missing", "null", "incomplete")):
        return MissionCategory.MISSING
    if any(word in text for word in ("consisten", "contradict", "cross-column")):
        return MissionCategory.CONSISTENCY
    return MissionCategory.GENERAL


def goal_signature(goal: str) -> str:
    tokens = sorted(_tokens(goal))
    payload = " ".join(tokens)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dataset_signature(mission: Mission) -> DatasetSignature:
    profile = mission.dataset_profile
    if profile is None:
        empty = DatasetSignature()
        empty.fingerprint = _hash("empty")
        return empty
    names = [column.name.strip().lower() for column in profile.columns]
    types = [column.inferred_type.strip().lower() for column in profile.columns]
    has_numeric = any(item == "numeric" for item in types)
    has_categorical = any(item in {"categorical", "text", "string"} for item in types)
    missing = _missingness(profile.columns)
    bucket = _row_bucket(profile.row_count)
    fingerprint = _hash(
        "|".join(
            [
                ",".join(names),
                ",".join(types),
                bucket.value,
                str(has_numeric),
                str(has_categorical),
                missing.value,
            ]
        )
    )
    return DatasetSignature(
        column_names=names,
        column_types=types,
        row_bucket=bucket,
        has_numeric=has_numeric,
        has_categorical=has_categorical,
        missingness=missing,
        fingerprint=fingerprint,
    )


def dataset_characteristics(signature: DatasetSignature) -> DatasetCharacteristics:
    return DatasetCharacteristics(
        has_numeric=signature.has_numeric,
        has_categorical=signature.has_categorical,
        missingness=signature.missingness,
        row_bucket=signature.row_bucket,
    )


def strategy_fingerprint(
    category: MissionCategory,
    characteristics: DatasetCharacteristics,
    capabilities: list[str],
) -> str:
    ordered = ",".join(sorted(capabilities))
    payload = (
        f"{category.value}|{int(characteristics.has_numeric)}|"
        f"{int(characteristics.has_categorical)}|{characteristics.missingness.value}|"
        f"{characteristics.row_bucket.value}|{ordered}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def experience_fingerprint(mission_id: str) -> str:
    return hashlib.sha256(f"experience|{mission_id}".encode("utf-8")).hexdigest()


def categories_related(left: MissionCategory, right: MissionCategory) -> bool:
    if left == right:
        return True
    return right in RELATED_CATEGORIES.get(left, frozenset())


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall((text or "").lower()))


def _row_bucket(row_count: int) -> RowCountBucket:
    if row_count < 20:
        return RowCountBucket.XS
    if row_count < 100:
        return RowCountBucket.S
    if row_count < 1000:
        return RowCountBucket.M
    return RowCountBucket.L


def _missingness(columns) -> MissingnessBucket:
    if not columns:
        return MissingnessBucket.NONE
    peak = max(column.null_percent for column in columns)
    if peak < 1:
        return MissingnessBucket.NONE
    if peak < 10:
        return MissingnessBucket.LOW
    if peak < 30:
        return MissingnessBucket.MEDIUM
    return MissingnessBucket.HIGH


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
