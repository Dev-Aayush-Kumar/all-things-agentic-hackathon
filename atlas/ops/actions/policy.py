"""Evidence-driven action proposals. Labeled LOCAL_FALLBACK. Never executes actions."""

from __future__ import annotations

from atlas.domain.enums import ActionStatus, FindingCategory, PlannerSource, StepStatus
from atlas.domain.models import Mission, SpecialistFollowUp
from atlas.investigation.missing import MATERIAL_MISSING_PERCENT
from atlas.ops.actions.registry import (
    ACTION_CAPABILITIES,
    ACTION_FILL_MISSING_VALUES,
    ACTION_REMOVE_DUPLICATES,
    CAPABILITY_FILL_MISSING,
    CAPABILITY_REMOVE_DUPLICATES,
)
from atlas.ops.workspace import MissionWorkspace

_REMEDIATION_PHRASES = (
    "fix the",
    "fix this",
    "fix major",
    "fix serious",
    "fix quality",
    "remediat",
    "clean this",
    "clean the dataset",
    "repair the",
    "remove duplicate",
    "fill missing",
)


def goal_requests_remediation(goal: str) -> bool:
    """True only when the mission asks ATLAS to change the dataset."""
    text = goal.lower()
    if "should be fixed first" in text:
        return False
    return any(phrase in text for phrase in _REMEDIATION_PHRASES)


def propose_action_follow_ups(workspace: MissionWorkspace) -> list[SpecialistFollowUp]:
    """Propose at most one evidence-justified action. Does not execute it."""
    mission = workspace.mission
    if not goal_requests_remediation(mission.goal):
        return []
    plan = mission.delegation_plan
    if plan is None:
        return []
    if _has_open_action_task(mission):
        return []
    if _verified_action_count(mission) >= workspace.settings.max_mission_actions:
        return []

    duplicates = _active_duplicate_count(mission)
    if duplicates > 0 and not _action_already_considered(mission, ACTION_REMOVE_DUPLICATES):
        return [
            SpecialistFollowUp(
                capability=CAPABILITY_REMOVE_DUPLICATES,
                objective=f"Remove {duplicates} exact duplicate row(s) from the working copy",
                arguments={"action_type": ACTION_REMOVE_DUPLICATES, "parameters": {}},
                reason=(
                    f"Evidence shows {duplicates} duplicate row(s) and the mission "
                    "asked for remediation."
                ),
            )
        ]

    column, missing_percent = _worst_material_missing(mission)
    if (
        column
        and not _fill_already_considered(mission, column)
    ):
        return [
            SpecialistFollowUp(
                capability=CAPABILITY_FILL_MISSING,
                objective=(
                    f"Fill missing values in '{column}' "
                    f"({missing_percent:.1f}% incomplete)"
                ),
                arguments={
                    "action_type": ACTION_FILL_MISSING_VALUES,
                    "parameters": {"column_name": column, "strategy": "auto"},
                },
                reason=(
                    f"Column '{column}' is materially incomplete "
                    f"({missing_percent:.1f}%) and the mission asked for remediation."
                ),
            )
        ]
    return []


def proposal_source() -> PlannerSource:
    return PlannerSource.LOCAL_FALLBACK


def _has_open_action_task(mission: Mission) -> bool:
    plan = mission.delegation_plan
    if plan is None:
        return False
    return any(
        task.capability in ACTION_CAPABILITIES
        and task.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
        for task in plan.tasks
    )


def _verified_action_count(mission: Mission) -> int:
    return sum(
        1
        for item in mission.actions
        if item.status in {ActionStatus.VERIFIED, ActionStatus.COMPLETED}
    )


def _action_already_considered(mission: Mission, action_type: str) -> bool:
    return any(item.action_type == action_type for item in mission.actions)


def _fill_already_considered(mission: Mission, column: str) -> bool:
    return any(
        item.action_type == ACTION_FILL_MISSING_VALUES
        and item.parameters.get("column_name") == column
        for item in mission.actions
    )


def _active_duplicate_count(mission: Mission) -> int:
    for finding in mission.findings:
        if finding.category != FindingCategory.DUPLICATE_ROWS:
            continue
        count = finding.evidence.get("duplicate_row_count")
        if isinstance(count, (int, float)) and count > 0:
            return int(count)
    return 0


def _worst_material_missing(mission: Mission) -> tuple[str | None, float]:
    best_column: str | None = None
    best_percent = 0.0
    for finding in mission.findings:
        if finding.category != FindingCategory.MISSING_DATA:
            continue
        percent = finding.evidence.get("missing_percent")
        material = finding.evidence.get("materially_incomplete") is True
        if not isinstance(percent, (int, float)):
            continue
        if not material and percent < MATERIAL_MISSING_PERCENT:
            continue
        if percent >= best_percent and finding.affected_columns:
            best_percent = float(percent)
            best_column = finding.affected_columns[0]
    return best_column, best_percent
