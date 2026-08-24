"""Validate, fingerprint, and merge memories. Memory is data, not a tool."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from atlas.config.settings import Settings
from atlas.domain.enums import MemoryExtractionSource, MemoryScope, MemoryType
from atlas.domain.exceptions import MemoryValidationError
from atlas.domain.models import MemoryProposal, MemoryProvenance, MemoryRecord, utc_now

CONFIDENCE_DETERMINISTIC = 0.80
CONFIDENCE_LOCAL_INSIGHT = 0.70
CONFIDENCE_GEMINI = 0.55
CONFIDENCE_MAX = 0.95
CONFIDENCE_MERGE_STEP = 0.05

ALLOWED_METADATA_KEYS = frozenset({"category", "tool_name", "finding_category", "column"})
SECRET_PATTERNS = (
    re.compile(r"api[_-]?key", re.I),
    re.compile(r"password", re.I),
    re.compile(r"secret", re.I),
    re.compile(r"BEGIN (RSA |OPENSSH )?PRIVATE KEY"),
    re.compile(r"GOOGLE_API_KEY", re.I),
    re.compile(r"Bearer\s+\S+", re.I),
)
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
WHITESPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def fingerprint_for(
    memory_type: MemoryType,
    content: str,
    scope: MemoryScope,
    scope_ref: str,
) -> str:
    normalized = _normalize_content(content)
    payload = f"{memory_type.value}|{normalized}|{scope.value}|{scope_ref.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_confidence(source: MemoryExtractionSource) -> float:
    if source == MemoryExtractionSource.DETERMINISTIC_EVIDENCE:
        return CONFIDENCE_DETERMINISTIC
    if source == MemoryExtractionSource.GEMINI_ADK:
        return CONFIDENCE_GEMINI
    return CONFIDENCE_LOCAL_INSIGHT


def clamp_confidence(value: float, source: MemoryExtractionSource) -> float:
    ceiling = (
        CONFIDENCE_GEMINI if source == MemoryExtractionSource.GEMINI_ADK else CONFIDENCE_MAX
    )
    return max(0.0, min(ceiling, float(value)))


def resolve_scope(
    proposal: MemoryProposal,
    *,
    mission_id: str,
    dataset_id: str | None,
) -> tuple[MemoryScope, str]:
    if proposal.type == MemoryType.FACT:
        if dataset_id:
            return MemoryScope.DATASET, dataset_id
        return MemoryScope.MISSION, mission_id
    if proposal.type == MemoryType.PREFERENCE:
        return MemoryScope.MISSION, mission_id
    if proposal.scope == MemoryScope.GLOBAL:
        return MemoryScope.GLOBAL, ""
    if proposal.scope == MemoryScope.DATASET and dataset_id:
        return MemoryScope.DATASET, dataset_id
    if proposal.scope == MemoryScope.MISSION:
        return MemoryScope.MISSION, mission_id
    if dataset_id:
        return MemoryScope.DATASET, dataset_id
    return MemoryScope.MISSION, mission_id


def validate_proposal(
    proposal: MemoryProposal,
    *,
    mission_id: str,
    dataset_id: str | None,
    settings: Settings,
    source: MemoryExtractionSource,
) -> MemoryRecord:
    content = (proposal.content or "").strip()
    if not content:
        raise MemoryValidationError("Memory content is required")
    if len(content) > settings.memory_content_max_chars:
        raise MemoryValidationError("Memory content exceeds the size limit")
    _reject_secrets(content)
    tags = _validate_tags(proposal.tags)
    metadata = _validate_metadata(proposal.metadata)
    scope, scope_ref = resolve_scope(proposal, mission_id=mission_id, dataset_id=dataset_id)
    if proposal.type == MemoryType.FACT and scope == MemoryScope.GLOBAL:
        raise MemoryValidationError("FACT memories cannot be global")
    confidence = default_confidence(source)
    if proposal.confidence is not None:
        confidence = clamp_confidence(proposal.confidence, source)
    provenance = MemoryProvenance(
        mission_id=mission_id,
        evidence_ids=list(proposal.evidence_ids),
        finding_ids=list(proposal.finding_ids),
        source_type=source,
    )
    return MemoryRecord(
        fingerprint=fingerprint_for(proposal.type, content, scope, scope_ref),
        type=proposal.type,
        content=content,
        scope=scope,
        scope_ref=scope_ref,
        tags=tags,
        confidence=confidence,
        provenance=[provenance],
        metadata=metadata,
        extraction_source=source,
    )


def merge_records(existing: MemoryRecord, incoming: MemoryRecord) -> MemoryRecord:
    """Combine support. Never drop earlier provenance."""
    known_missions = {item.mission_id for item in existing.provenance}
    added = False
    for item in incoming.provenance:
        if item.mission_id in known_missions:
            _merge_ids(existing, item)
            continue
        existing.provenance.append(item)
        known_missions.add(item.mission_id)
        added = True
    if added:
        existing.confidence = min(
            CONFIDENCE_MAX, existing.confidence + CONFIDENCE_MERGE_STEP
        )
    elif incoming.confidence > existing.confidence:
        existing.confidence = incoming.confidence
    existing.tags = sorted(set(existing.tags) | set(incoming.tags))
    existing.updated_at = utc_now()
    return existing


def _merge_ids(existing: MemoryRecord, item: MemoryProvenance) -> None:
    for record in existing.provenance:
        if record.mission_id != item.mission_id:
            continue
        record.evidence_ids = list(dict.fromkeys([*record.evidence_ids, *item.evidence_ids]))
        record.finding_ids = list(dict.fromkeys([*record.finding_ids, *item.finding_ids]))


def _normalize_content(content: str) -> str:
    text = WHITESPACE_RE.sub(" ", content.strip().lower())
    return PUNCT_RE.sub("", text)


def _reject_secrets(content: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise MemoryValidationError("Memory content looks like a secret or credential")


def _validate_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in tags[:8]:
        tag = raw.strip().lower()
        if not tag:
            continue
        if not TAG_RE.match(tag):
            raise MemoryValidationError(f"Invalid memory tag '{raw}'")
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def _validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    extra = set(metadata) - ALLOWED_METADATA_KEYS
    if extra:
        raise MemoryValidationError(f"Memory metadata keys are not allowed: {sorted(extra)}")
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str) and len(value) > 120:
                raise MemoryValidationError("Memory metadata value is too long")
            safe[key] = value
            continue
        raise MemoryValidationError("Memory metadata values must be scalars")
    return safe
