"""Map retrieved strategies onto allowlisted observation follow-ups.

A strategy never executes work. It only suggests catalog capabilities that
current evidence and the current dataset can support. Actions, external tools,
and forbidden names are ignored here — those still require their own policies.
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
from atlas.domain.models import SpecialistFollowUp
from atlas.ops.learning.policy import INFLUENCEABLE_CAPABILITIES
from atlas.ops.planning import task_exists
from atlas.ops.workspace import MissionWorkspace

_OBJECTIVES = {
    PROFILE_DATASET: "Profile the dataset as recommended by historical strategy",
    ANALYZE_MISSING: "Measure missingness recommended by historical strategy",
    ANALYZE_DUPLICATES: "Measure duplicate rows recommended by historical strategy",
    ANALYZE_TYPE_FORMAT: "Measure type/format issues recommended by historical strategy",
    ANALYZE_OUTLIERS: "Measure numeric outliers recommended by historical strategy",
    ANALYZE_CONSISTENCY: "Measure consistency recommended by historical strategy",
}


def strategy_follow_ups(workspace: MissionWorkspace) -> list[SpecialistFollowUp]:
    """Suggest allowlisted observations justified by retrieved strategies."""
    plan = workspace.mission.delegation_plan
    if plan is None or workspace.mission.dataset_profile is None:
        return []
    follow_ups: list[SpecialistFollowUp] = []
    seen: set[str] = set()
    influenced: list[str] = []
    for record in workspace.retrieved_strategies:
        for capability in record.recommended_capabilities:
            if capability not in INFLUENCEABLE_CAPABILITIES:
                continue
            if capability in seen:
                continue
            if task_exists(plan, capability):
                continue
            if not _dataset_supports(workspace, capability):
                continue
            seen.add(capability)
            follow_ups.append(
                SpecialistFollowUp(
                    capability=capability,
                    objective=_OBJECTIVES.get(capability, f"Measure {capability}"),
                    arguments={"adaptive": True},
                    reason=(
                        "Historical strategy "
                        f"{record.strategy_id[:8]} recommended {capability} "
                        f"(success_rate={record.success_rate:.2f}, "
                        f"n={record.historical_runs})"
                    ),
                )
            )
            if record.strategy_id not in influenced:
                influenced.append(record.strategy_id)
    if influenced:
        existing = list(workspace.mission.strategy_ids_influenced)
        for item in influenced:
            if item not in existing:
                existing.append(item)
        workspace.mission.strategy_ids_influenced = existing
    return follow_ups


def decision_capabilities(decision) -> list[str]:
    names: list[str] = []
    if getattr(decision, "tasks", None):
        names.extend(item.capability for item in decision.tasks if item.capability)
    tool = getattr(decision, "tool", None)
    if tool is not None and getattr(tool, "name", None):
        names.append(tool.name)
    action = getattr(decision, "action", None)
    if action is not None and getattr(action, "type", None):
        names.append(action.type)
    external = getattr(decision, "external", None)
    if external is not None and getattr(external, "capability", None):
        names.append(external.capability)
    return names


def note_strategy_influence(workspace: MissionWorkspace, decision) -> None:
    """Record which retrieved strategies overlap an accepted decision."""
    caps = set(decision_capabilities(decision))
    if not caps:
        return
    existing = list(workspace.mission.strategy_ids_influenced)
    for record in workspace.retrieved_strategies:
        if caps & set(record.recommended_capabilities):
            if record.strategy_id not in existing:
                existing.append(record.strategy_id)
    workspace.mission.strategy_ids_influenced = existing


def _dataset_supports(workspace: MissionWorkspace, capability: str) -> bool:
    profile = workspace.mission.dataset_profile
    if profile is None:
        return False
    if capability == ANALYZE_OUTLIERS:
        return any(column.inferred_type == "numeric" for column in profile.columns)
    if capability == ANALYZE_CONSISTENCY:
        return profile.column_count >= 2
    return True
