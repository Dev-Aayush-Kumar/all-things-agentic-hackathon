"""Deterministic tool-selection and adaptive-decision policy.

This is the local-development brain. It does not call Gemini.
ADK may propose additional tools; those proposals are still filtered
through the same allowlist and adaptive evidence rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atlas.agent.tools import (
    ANALYZE_CONSISTENCY,
    ANALYZE_DUPLICATES,
    ANALYZE_MISSING,
    ANALYZE_OUTLIERS,
    ANALYZE_TYPE_FORMAT,
    INSPECT_COLUMN,
    INVESTIGATION_TOOLS,
    PROFILE_DATASET,
    ToolResult,
)
from atlas.domain.models import AgentTask, DatasetProfile, Finding
from atlas.investigation.missing import MATERIAL_MISSING_PERCENT


@dataclass(frozen=True)
class AdaptiveAction:
    """A follow-up tool call justified by observed evidence."""

    tool_name: str
    arguments: dict[str, Any]
    reason: str


def available_tools() -> tuple[str, ...]:
    return INVESTIGATION_TOOLS


def select_tools(goal: str) -> list[str]:
    """Choose investigation tools from the mission goal. Never includes inspect_column."""
    text = goal.lower()
    selected: list[str] = [PROFILE_DATASET]

    broad = any(
        word in text
        for word in (
            "quality",
            "problem",
            "inconsist",
            "anomal",
            "investigat",
            "issue",
            "clean",
            "fix",
            "resolution",
        )
    )

    def add(tool: str, keywords: tuple[str, ...]) -> None:
        if tool not in selected and (broad or any(word in text for word in keywords)):
            selected.append(tool)

    add(ANALYZE_MISSING, ("missing", "null", "incomplete", "empty"))
    add(ANALYZE_DUPLICATES, ("duplicate", "duplicated", "repeat"))
    add(ANALYZE_TYPE_FORMAT, ("type", "format", "invalid", "schema", "coercion"))
    add(ANALYZE_OUTLIERS, ("outlier", "extreme", "numeric"))
    add(ANALYZE_CONSISTENCY, ("consisten", "contradict", "cross-column", "cross column"))

    if selected == [PROFILE_DATASET] and any(
        word in text for word in ("analy", "inspect", "dataset", "data", "csv", "review")
    ):
        selected.extend([ANALYZE_MISSING, ANALYZE_DUPLICATES, ANALYZE_TYPE_FORMAT])

    return selected


def understand_goal(goal: str) -> str:
    """Short operational restatement of the mission (not model chain-of-thought)."""
    return f"Investigate the attached dataset to satisfy: {goal.strip()}"


def tasks_from_tools(
    tools: list[str],
    *,
    adaptive: bool = False,
    arguments_by_tool: dict[str, dict] | None = None,
    reason: str | None = None,
    start_index: int = 1,
) -> list[AgentTask]:
    """Build ordered tasks. Profile is always first among initial tools."""
    args_map = arguments_by_tool or {}
    ordered = list(tools)
    if PROFILE_DATASET in ordered:
        ordered = [PROFILE_DATASET] + [tool for tool in ordered if tool != PROFILE_DATASET]

    tasks: list[AgentTask] = []
    previous_id: str | None = None
    for offset, tool in enumerate(ordered):
        task_id = f"task_{start_index + offset}"
        tasks.append(
            AgentTask(
                task_id=task_id,
                tool_name=tool,
                objective=_tool_objective(tool, args_map.get(tool, {})),
                depends_on=[previous_id] if previous_id else [],
                adaptive=adaptive,
                arguments=args_map.get(tool, {}),
                decision_reason=reason,
            )
        )
        previous_id = task_id
    return tasks


def decide_adaptive_actions(
    *,
    completed_tools: set[str],
    results: list[ToolResult],
    inspected_columns: set[str],
    planned_tools: set[str],
) -> list[AdaptiveAction]:
    """Return extra work justified by actual tool output. Empty if nothing new is warranted."""
    actions: list[AdaptiveAction] = []
    profile = next((item.profile for item in results if item.profile is not None), None)

    if profile is not None and ANALYZE_OUTLIERS not in planned_tools:
        extreme = _extreme_numeric_columns(profile)
        if extreme:
            actions.append(
                AdaptiveAction(
                    tool_name=ANALYZE_OUTLIERS,
                    arguments={},
                    reason=(
                        "Dataset profile shows extreme numeric range in "
                        f"{', '.join(extreme)}; outlier analysis was not originally selected."
                    ),
                )
            )

    missing_result = next(
        (item for item in results if item.tool_name == ANALYZE_MISSING), None
    )
    if missing_result is not None:
        for finding in missing_result.findings:
            if not _is_material_missing(finding):
                continue
            column = finding.affected_columns[0] if finding.affected_columns else None
            if column and column not in inspected_columns:
                actions.append(
                    AdaptiveAction(
                        tool_name=INSPECT_COLUMN,
                        arguments={"column_name": column},
                        reason=(
                            f"Missing-value analysis found material incompleteness in '{column}' "
                            f"({finding.evidence.get('missing_percent')}%)."
                        ),
                    )
                )

    type_result = next(
        (item for item in results if item.tool_name == ANALYZE_TYPE_FORMAT), None
    )
    if type_result is not None and type_result.findings:
        column = type_result.findings[0].affected_columns[0]
        if column not in inspected_columns:
            actions.append(
                AdaptiveAction(
                    tool_name=INSPECT_COLUMN,
                    arguments={"column_name": column},
                    reason=(
                        f"Type/format analysis found anomalies in '{column}'; "
                        "inspecting that column in more detail."
                    ),
                )
            )

    deduped: list[AdaptiveAction] = []
    seen: set[tuple] = set()
    for action in actions:
        if action.tool_name in planned_tools and action.tool_name != INSPECT_COLUMN:
            continue
        key = (action.tool_name, tuple(sorted(action.arguments.items())))
        if key in seen:
            continue
        if action.tool_name == INSPECT_COLUMN:
            column = action.arguments.get("column_name")
            if column in inspected_columns:
                continue
        seen.add(key)
        deduped.append(action)
    return deduped


def _is_material_missing(finding: Finding) -> bool:
    if finding.evidence.get("materially_incomplete") is True:
        return True
    percent = finding.evidence.get("missing_percent")
    return isinstance(percent, (int, float)) and percent >= MATERIAL_MISSING_PERCENT


def _extreme_numeric_columns(profile: DatasetProfile) -> list[str]:
    names: list[str] = []
    for column in profile.columns:
        stats = column.numeric_stats
        if stats is None or stats.median <= 0:
            continue
        if stats.max > 10 * stats.median:
            names.append(column.name)
    return names


def _tool_objective(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == INSPECT_COLUMN:
        column = arguments.get("column_name", "selected column")
        return f"Inspect column '{column}' in more detail using measured statistics"
    labels = {
        PROFILE_DATASET: "Profile dataset shape, types, and numeric statistics",
        ANALYZE_MISSING: "Measure missing values per column",
        ANALYZE_DUPLICATES: "Count exact duplicate rows",
        ANALYZE_TYPE_FORMAT: "Detect type/format anomalies",
        ANALYZE_OUTLIERS: "Detect numeric outliers with IQR where appropriate",
        ANALYZE_CONSISTENCY: "Run explicit cross-column consistency rules",
    }
    return labels.get(tool_name, f"Run capability {tool_name}")
