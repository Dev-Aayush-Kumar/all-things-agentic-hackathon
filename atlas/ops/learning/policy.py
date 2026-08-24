"""Allowlist, confidence, and merge rules for strategy learning.

Strategies are advisory data. They cannot grant capabilities or rewrite ATLAS.
"""

from __future__ import annotations

from atlas.agent.tools import (
    ANALYZE_CONSISTENCY,
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    ANALYZE_TYPE_FORMAT,
    PROFILE_DATASET,
)
from atlas.domain.enums import ExperienceOutcome
from atlas.domain.exceptions import StrategyValidationError
from atlas.domain.models import ExperienceRecord, StrategyRecord, utc_now
from atlas.ops.actions.registry import ACTION_FILL_MISSING_VALUES, ACTION_REMOVE_DUPLICATES
from atlas.ops.capabilities import FORBIDDEN_CAPABILITIES
from atlas.ops.external.registry import CAPABILITY_FETCH_URL
from atlas.ops.learning.signatures import strategy_fingerprint

CONFIDENCE_MAX = 0.95
SUPPORTING_MISSION_LIMIT = 20

OBSERVATION_CAPABILITIES = frozenset(
    {
        PROFILE_DATASET,
        ANALYZE_MISSING,
        ANALYZE_DUPLICATES,
        ANALYZE_TYPE_FORMAT,
        ANALYZE_OUTLIERS,
        ANALYZE_CONSISTENCY,
    }
)
ACTION_CAPABILITY_NAMES = frozenset(
    {ACTION_REMOVE_DUPLICATES, ACTION_FILL_MISSING_VALUES}
)
EXTERNAL_CAPABILITY_NAMES = frozenset({CAPABILITY_FETCH_URL})
PERSISTABLE_CAPABILITIES = (
    OBSERVATION_CAPABILITIES | ACTION_CAPABILITY_NAMES | EXTERNAL_CAPABILITY_NAMES
)
INFLUENCEABLE_CAPABILITIES = OBSERVATION_CAPABILITIES


def sample_factor(runs: int) -> float:
    """Sample-size factor. One run stays low; confidence cannot grow without bound."""
    if runs <= 0:
        return 0.0
    if runs == 1:
        return 0.40
    if runs == 2:
        return 0.70
    if runs == 3:
        return 0.80
    return min(0.92, 0.80 + 0.03 * (runs - 3))


def quality_score(
    success_rate: float, average_evidence: float, average_efficiency: float
) -> float:
    return (
        0.45 * _clamp(success_rate)
        + 0.35 * _clamp(average_evidence)
        + 0.20 * _clamp(average_efficiency)
    )


def compute_confidence(
    *,
    runs: int,
    success_rate: float,
    average_evidence: float,
    average_efficiency: float,
) -> float:
    """Bounded confidence. Documented in README. Not model-reported truth."""
    quality = quality_score(success_rate, average_evidence, average_efficiency)
    return min(CONFIDENCE_MAX, sample_factor(runs) * quality)


def sanitize_capabilities(names: list[str]) -> list[str]:
    """Drop forbidden and unknown names. Preserve first-seen observation order."""
    cleaned: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if not name or name in FORBIDDEN_CAPABILITIES:
            continue
        if name not in PERSISTABLE_CAPABILITIES:
            continue
        if name not in cleaned:
            cleaned.append(name)
    if PROFILE_DATASET in cleaned:
        cleaned = [PROFILE_DATASET, *[item for item in cleaned if item != PROFILE_DATASET]]
    return cleaned


def recommendable_observations(names: list[str]) -> list[str]:
    return [item for item in sanitize_capabilities(names) if item in INFLUENCEABLE_CAPABILITIES]


def assert_strategy_safe(record: StrategyRecord) -> None:
    forbidden = [item for item in record.recommended_capabilities if item in FORBIDDEN_CAPABILITIES]
    if forbidden:
        raise StrategyValidationError(
            f"Strategy cannot recommend forbidden capabilities: {forbidden}"
        )
    unknown = [
        item
        for item in record.recommended_capabilities
        if item not in PERSISTABLE_CAPABILITIES
    ]
    if unknown:
        raise StrategyValidationError(
            f"Strategy capabilities are not in the allowlisted catalog: {unknown}"
        )


def merge_strategy(existing: StrategyRecord, experience: ExperienceRecord) -> StrategyRecord:
    """Update running averages. Never drop earlier supporting missions."""
    if experience.mission_id in existing.supporting_mission_ids:
        existing.updated_at = utc_now()
        return existing
    n_old = existing.historical_runs
    n_new = n_old + 1
    existing.success_rate = _running_mean(existing.success_rate, experience.success_score, n_old)
    existing.average_efficiency = _running_mean(
        existing.average_efficiency, experience.efficiency_score, n_old
    )
    existing.average_evidence_score = _running_mean(
        existing.average_evidence_score, experience.evidence_score, n_old
    )
    existing.historical_runs = n_new
    if experience.outcome == ExperienceOutcome.FAILURE:
        existing.failure_count += 1
    existing.supporting_mission_ids = [
        *existing.supporting_mission_ids,
        experience.mission_id,
    ][-SUPPORTING_MISSION_LIMIT:]
    existing.confidence = compute_confidence(
        runs=existing.historical_runs,
        success_rate=existing.success_rate,
        average_evidence=existing.average_evidence_score,
        average_efficiency=existing.average_efficiency,
    )
    existing.fingerprint = strategy_fingerprint(
        existing.mission_category,
        existing.dataset_characteristics,
        existing.recommended_capabilities,
    )
    existing.updated_at = utc_now()
    assert_strategy_safe(existing)
    return existing


def strategy_from_experience(experience: ExperienceRecord) -> StrategyRecord | None:
    recommended = recommendable_observations(experience.strategy_steps or experience.tools_used)
    if not recommended:
        return None
    from atlas.ops.learning.signatures import dataset_characteristics

    chars = dataset_characteristics(experience.dataset_signature)
    record = StrategyRecord(
        fingerprint=strategy_fingerprint(experience.mission_category, chars, recommended),
        mission_category=experience.mission_category,
        dataset_characteristics=chars,
        recommended_capabilities=recommended,
        historical_runs=1,
        success_rate=experience.success_score,
        average_efficiency=experience.efficiency_score,
        average_evidence_score=experience.evidence_score,
        failure_count=1 if experience.outcome == ExperienceOutcome.FAILURE else 0,
        supporting_mission_ids=[experience.mission_id],
    )
    record.confidence = compute_confidence(
        runs=1,
        success_rate=record.success_rate,
        average_evidence=record.average_evidence_score,
        average_efficiency=record.average_efficiency,
    )
    assert_strategy_safe(record)
    return record


def _running_mean(current: float, incoming: float, n_old: int) -> float:
    if n_old <= 0:
        return incoming
    return ((current * n_old) + incoming) / (n_old + 1)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
