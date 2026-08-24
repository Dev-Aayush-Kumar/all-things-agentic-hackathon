"""Map retrieved memories onto allowlisted follow-ups. Never execute memory text."""

from __future__ import annotations

from atlas.agent.tools import ANALYZE_CONSISTENCY, ANALYZE_MISSING, ANALYZE_OUTLIERS
from atlas.domain.enums import MemoryType
from atlas.domain.models import SpecialistFollowUp
from atlas.ops.planning import task_exists
from atlas.ops.workspace import MissionWorkspace

_OUTLIER_HINTS = ("outlier", "extreme numeric", "extreme range", "iqr")
_MISSING_HINTS = ("missing value", "null count", "incomplete")
_CONSISTENCY_HINTS = ("consistency", "cross-column")


def memory_follow_ups(workspace: MissionWorkspace) -> list[SpecialistFollowUp]:
    """Suggest allowlisted observations justified by memory and current evidence."""
    plan = workspace.mission.delegation_plan
    if plan is None:
        return []
    if workspace.mission.dataset_profile is None:
        return []
    follow_ups: list[SpecialistFollowUp] = []
    for record in workspace.retrieved_memories:
        if record.type not in {MemoryType.INSIGHT, MemoryType.PROCEDURE}:
            continue
        text = f"{record.content} {' '.join(record.tags)}".lower()
        if _mentions(text, _OUTLIER_HINTS) and _has_numeric_columns(workspace):
            if not task_exists(plan, ANALYZE_OUTLIERS):
                follow_ups.append(
                    SpecialistFollowUp(
                        capability=ANALYZE_OUTLIERS,
                        objective="Measure numeric outliers suggested by historical memory",
                        arguments={"adaptive": True},
                        reason=(
                            "Retrieved memory advises that duplicate analysis can miss "
                            "extreme numeric anomalies"
                        ),
                    )
                )
        if _mentions(text, _MISSING_HINTS):
            if not task_exists(plan, ANALYZE_MISSING):
                follow_ups.append(
                    SpecialistFollowUp(
                        capability=ANALYZE_MISSING,
                        objective="Measure missingness suggested by historical memory",
                        arguments={"adaptive": True},
                        reason="Retrieved memory recommends missing-value analysis",
                    )
                )
        if _mentions(text, _CONSISTENCY_HINTS):
            if not task_exists(plan, ANALYZE_CONSISTENCY):
                follow_ups.append(
                    SpecialistFollowUp(
                        capability=ANALYZE_CONSISTENCY,
                        objective="Measure consistency suggested by historical memory",
                        arguments={"adaptive": True},
                        reason="Retrieved memory recommends consistency checks",
                    )
                )
    return follow_ups


def _mentions(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _has_numeric_columns(workspace: MissionWorkspace) -> bool:
    profile = workspace.mission.dataset_profile
    if profile is None:
        return False
    return any(column.inferred_type == "numeric" for column in profile.columns)
