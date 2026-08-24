"""Post-completion memory extraction. Failures never fail the mission."""

from __future__ import annotations

import logging

from atlas.config.settings import Settings
from atlas.domain.enums import (
    EventType,
    FindingCategory,
    MemoryExtractionSource,
    MemoryScope,
    MemoryType,
    PlannerSource,
)
from atlas.domain.exceptions import MemoryValidationError
from atlas.domain.models import MemoryProposal, Mission, MissionEvent
from atlas.ops.memory.policy import validate_proposal
from atlas.persistence.memory_base import MemoryRepository

logger = logging.getLogger(__name__)

OUTLIER_INSIGHT = (
    "Duplicate analysis alone can miss extreme numeric anomalies that "
    "ordinary row-deduplication does not surface."
)


class LocalMemoryExtractor:
    """Deterministic extraction from structured findings. Always LOCAL_FALLBACK."""

    @property
    def source(self) -> PlannerSource:
        return PlannerSource.LOCAL_FALLBACK

    def propose(self, mission: Mission) -> list[MemoryProposal]:
        proposals: list[MemoryProposal] = []
        outlier_findings = [
            item
            for item in mission.findings
            if item.category == FindingCategory.NUMERIC_OUTLIER
        ]
        duplicate_findings = [
            item
            for item in mission.findings
            if item.category == FindingCategory.DUPLICATE_ROWS
        ]
        evidence_ids = [record.evidence_id for record in mission.evidence_records]
        if outlier_findings:
            proposals.append(
                MemoryProposal(
                    type=MemoryType.INSIGHT,
                    content=OUTLIER_INSIGHT,
                    scope=MemoryScope.GLOBAL,
                    tags=["outliers", "duplicates", "investigation"],
                    evidence_ids=evidence_ids[:4],
                    finding_ids=[item.finding_id for item in outlier_findings[:3]],
                    metadata={"category": "investigation"},
                )
            )
            for finding in outlier_findings[:2]:
                column = finding.affected_columns[0] if finding.affected_columns else None
                if not column:
                    continue
                proposals.append(
                    MemoryProposal(
                        type=MemoryType.FACT,
                        content=(
                            f"For this dataset, column '{column}' contained numeric "
                            "values outside the IQR range used by ATLAS."
                        ),
                        scope=MemoryScope.DATASET,
                        tags=["outliers", "fact"],
                        evidence_ids=evidence_ids[:4],
                        finding_ids=[finding.finding_id],
                        metadata={"column": column, "finding_category": finding.category.value},
                    )
                )
        if outlier_findings or (
            "outlier" in mission.goal.lower() or "extreme" in mission.goal.lower()
        ):
            proposals.append(
                MemoryProposal(
                    type=MemoryType.PROCEDURE,
                    content=(
                        "When investigating survey or quality CSVs, run outlier "
                        "analysis after profiling; duplicate checks are not sufficient."
                    ),
                    scope=MemoryScope.GLOBAL,
                    tags=["outliers", "procedure", "survey"],
                    evidence_ids=evidence_ids[:4],
                    metadata={"tool_name": "analyze_outliers"},
                )
            )
        missing = [
            item
            for item in mission.findings
            if item.category == FindingCategory.MISSING_DATA
        ]
        if missing:
            column = missing[0].affected_columns[0] if missing[0].affected_columns else "a column"
            proposals.append(
                MemoryProposal(
                    type=MemoryType.FACT,
                    content=(
                        f"For this dataset, '{column}' had material missing values "
                        "measured during investigation."
                    ),
                    scope=MemoryScope.DATASET,
                    tags=["missing", "fact"],
                    evidence_ids=evidence_ids[:4],
                    finding_ids=[missing[0].finding_id],
                    metadata={"column": str(column), "finding_category": "MISSING_DATA"},
                )
            )
        if duplicate_findings and not outlier_findings:
            proposals.append(
                MemoryProposal(
                    type=MemoryType.PROCEDURE,
                    content=(
                        "When a goal emphasizes duplicates, still consider whether "
                        "extreme numeric values need a separate measurement."
                    ),
                    scope=MemoryScope.GLOBAL,
                    tags=["duplicates", "outliers", "procedure"],
                    evidence_ids=evidence_ids[:4],
                    finding_ids=[item.finding_id for item in duplicate_findings[:2]],
                )
            )
        return proposals


class ScriptedMemoryExtractor:
    """Test double. Never calls Gemini."""

    def __init__(self, proposals: list, *, source: PlannerSource = PlannerSource.GEMINI_ADK) -> None:
        self._proposals = list(proposals)
        self._source = source

    @property
    def source(self) -> PlannerSource:
        return self._source

    def propose(self, mission: Mission) -> list:
        del mission
        return list(self._proposals)


async def extract_and_store(
    mission: Mission,
    repository: MemoryRepository,
    settings: Settings,
    *,
    extractor=None,
) -> list:
    """Validate and persist proposals. Returns stored records. Never raises to caller."""
    stored: list = []
    if not settings.memory_enabled:
        return stored
    extractor = extractor or LocalMemoryExtractor()
    source = (
        MemoryExtractionSource.GEMINI_ADK
        if getattr(extractor, "source", None) == PlannerSource.GEMINI_ADK
        else MemoryExtractionSource.LOCAL_FALLBACK
    )
    if source == MemoryExtractionSource.LOCAL_FALLBACK and isinstance(
        extractor, LocalMemoryExtractor
    ):
        source = MemoryExtractionSource.DETERMINISTIC_EVIDENCE
        for proposal in extractor.propose(mission):
            if proposal.type in {MemoryType.INSIGHT, MemoryType.PROCEDURE}:
                item_source = MemoryExtractionSource.LOCAL_FALLBACK
            else:
                item_source = MemoryExtractionSource.DETERMINISTIC_EVIDENCE
            record = await _persist_one(
                mission, repository, settings, proposal, item_source
            )
            if record is not None:
                stored.append(record)
            if len(stored) >= settings.memory_max_extract:
                break
        return stored

    try:
        proposals = extractor.propose(mission)
        if hasattr(proposals, "__await__"):
            proposals = await proposals
    except Exception as exc:
        logger.exception("Memory extractor failed mission=%s", mission.mission_id)
        _add_event(
            mission,
            EventType.MEMORY_EXTRACTION_FAILED,
            "Memory extraction failed",
            {"error": str(exc), "source": source.value},
        )
        return stored

    for proposal in proposals[: settings.memory_max_extract]:
        if not isinstance(proposal, MemoryProposal):
            try:
                proposal = MemoryProposal.model_validate(proposal)
            except Exception as exc:
                _add_event(
                    mission,
                    EventType.MEMORY_REJECTED,
                    "Malformed memory proposal rejected",
                    {"error": str(exc), "source": source.value},
                )
                continue
        record = await _persist_one(mission, repository, settings, proposal, source)
        if record is not None:
            stored.append(record)
    return stored


async def _persist_one(
    mission: Mission,
    repository: MemoryRepository,
    settings: Settings,
    proposal: MemoryProposal,
    source: MemoryExtractionSource,
):
    try:
        candidate = validate_proposal(
            proposal,
            mission_id=mission.mission_id,
            dataset_id=mission.dataset_id,
            settings=settings,
            source=source,
        )
    except MemoryValidationError as exc:
        _add_event(
            mission,
            EventType.MEMORY_REJECTED,
            "Memory proposal rejected",
            {"error": str(exc), "source": source.value},
        )
        return None
    existing = await repository.find_by_fingerprint(candidate.fingerprint)
    if existing is not None:
        merged = await repository.upsert(candidate)
        _add_event(
            mission,
            EventType.MEMORY_MERGED,
            "Existing memory updated with additional provenance",
            {
                "memory_id": merged.memory_id,
                "fingerprint": merged.fingerprint,
                "source": source.value,
            },
        )
        return merged
    stored = await repository.upsert(candidate)
    _add_event(
        mission,
        EventType.MEMORY_EXTRACTED,
        "Memory persisted",
        {
            "memory_id": stored.memory_id,
            "type": stored.type.value,
            "scope": stored.scope.value,
            "source": source.value,
        },
    )
    return stored


def _add_event(mission: Mission, event_type: EventType, message: str, metadata: dict) -> None:
    mission.events.append(MissionEvent(type=event_type, message=message, metadata=metadata))
    mission.touch()
