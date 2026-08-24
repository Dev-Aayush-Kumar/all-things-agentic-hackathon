"""Deterministic mission-outcome evaluation.

Scores are bounded to [0.0, 1.0]. More findings is not automatically better.
The formulas are explicit policy, not statistical inference.
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
from atlas.config.settings import Settings
from atlas.domain.enums import (
    ActionStatus,
    EvidenceSourceType,
    ExecutionState,
    ExperienceOutcome,
    MissionCategory,
    MissionStatus,
    StepStatus,
)
from atlas.domain.models import ExperienceRecord, Mission
from atlas.ops.learning.policy import sanitize_capabilities
from atlas.ops.learning.signatures import (
    classify_goal,
    dataset_signature,
    experience_fingerprint,
    goal_signature,
    strategy_fingerprint,
)
from atlas.ops.learning.signatures import dataset_characteristics as to_characteristics

GOAL_TOOLS = {
    MissionCategory.DATA_QUALITY: frozenset(
        {
            PROFILE_DATASET,
            ANALYZE_MISSING,
            ANALYZE_DUPLICATES,
            ANALYZE_TYPE_FORMAT,
            ANALYZE_OUTLIERS,
            ANALYZE_CONSISTENCY,
        }
    ),
    MissionCategory.DUPLICATES: frozenset({PROFILE_DATASET, ANALYZE_DUPLICATES}),
    MissionCategory.MISSING: frozenset({PROFILE_DATASET, ANALYZE_MISSING}),
    MissionCategory.OUTLIERS: frozenset({PROFILE_DATASET, ANALYZE_OUTLIERS}),
    MissionCategory.CONSISTENCY: frozenset({PROFILE_DATASET, ANALYZE_CONSISTENCY}),
    MissionCategory.GENERAL: frozenset({PROFILE_DATASET}),
}


def evaluate_mission(mission: Mission, settings: Settings) -> ExperienceRecord:
    """Build a compact experience from observable mission signals."""
    category = classify_goal(mission.goal)
    tools_used = _tools_used(mission)
    specialists_used = _specialists_used(mission)
    actions_used = _actions_used(mission)
    external_used = _external_used(mission)
    steps = sanitize_capabilities(tools_used)
    signature = dataset_signature(mission)
    chars = to_characteristics(signature)
    success = _success_score(mission)
    evidence = _evidence_score(mission, category, tools_used)
    efficiency = _efficiency_score(mission, settings)
    failures = _failure_count(mission)
    outcome = _outcome(mission, success)
    return ExperienceRecord(
        mission_id=mission.mission_id,
        dataset_signature=signature,
        goal_signature=goal_signature(mission.goal),
        mission_category=category,
        strategy_signature=strategy_fingerprint(category, chars, steps),
        strategy_steps=steps,
        tools_used=tools_used,
        specialists_used=specialists_used,
        actions_used=actions_used,
        external_tools_used=external_used,
        outcome=outcome,
        success_score=success,
        efficiency_score=efficiency,
        evidence_score=evidence,
        iterations=mission.reasoning_iteration,
        tool_calls=mission.agent_plan.tool_call_count if mission.agent_plan else 0,
        model_calls=mission.model_call_count,
        specialist_tasks=len(mission.delegation_plan.tasks) if mission.delegation_plan else 0,
        actions=len(mission.actions),
        failures=failures,
        fingerprint=experience_fingerprint(mission.mission_id),
    )


def _success_score(mission: Mission) -> float:
    failed_state = mission.execution.state in {ExecutionState.FAILED, ExecutionState.EXHAUSTED}
    if mission.status == MissionStatus.FAILED or failed_state:
        score = 0.0
    elif _finished_successfully(mission):
        score = 0.75
    else:
        score = 0.35
    failed_tasks = _failed_tasks(mission)
    failed_actions = _failed_actions(mission)
    if _finished_successfully(mission) and failed_tasks == 0:
        score += 0.15
    if _finished_successfully(mission) and failed_actions == 0:
        score += 0.10
    score -= min(0.30, 0.05 * failed_tasks)
    score -= min(0.20, 0.10 * failed_actions)
    return _clamp(score)


def _evidence_score(mission: Mission, category: MissionCategory, tools_used: list[str]) -> float:
    has_profile = 0.30 if mission.dataset_profile is not None else 0.0
    dataset_evidence = [
        item
        for item in mission.evidence_records
        if item.source_type == EvidenceSourceType.DATASET
    ]
    successful = [
        item
        for item in dataset_evidence
        if (item.execution_status or "").upper() != "FAILED"
    ]
    has_evidence = 0.25 if successful else 0.0
    expected = GOAL_TOOLS.get(category, frozenset({PROFILE_DATASET}))
    coverage = 0.25 if expected & set(tools_used) else 0.0
    tasks = mission.delegation_plan.tasks if mission.delegation_plan else []
    if tasks:
        completed = sum(1 for item in tasks if item.status == StepStatus.COMPLETED)
        specialist = 0.20 * (completed / len(tasks))
    else:
        specialist = 0.0
    return _clamp(has_profile + has_evidence + coverage + specialist)


def _efficiency_score(mission: Mission, settings: Settings) -> float:
    iterations = mission.reasoning_iteration
    tool_calls = mission.agent_plan.tool_call_count if mission.agent_plan else 0
    model_calls = mission.model_call_count
    iter_h = 1.0 - min(1.0, iterations / max(1, settings.agent_max_iterations))
    tool_h = 1.0 - min(1.0, tool_calls / max(1, settings.agent_max_tool_calls))
    model_h = 1.0 - min(1.0, model_calls / max(1, settings.max_model_calls))
    fail_pen = min(1.0, _failure_count(mission) / 4)
    repeated = _repeated_count(mission)
    repeat_pen = min(1.0, repeated / max(1, settings.max_repeated_decisions))
    retries = 0
    if mission.delegation_plan:
        retries = sum(max(0, item.attempt_count - 1) for item in mission.delegation_plan.tasks)
    retry_pen = min(1.0, retries / 4)
    return _clamp(
        0.30 * iter_h
        + 0.25 * tool_h
        + 0.15 * model_h
        + 0.15 * (1.0 - fail_pen)
        + 0.10 * (1.0 - repeat_pen)
        + 0.05 * (1.0 - retry_pen)
    )


def _outcome(mission: Mission, success: float) -> ExperienceOutcome:
    if mission.status == MissionStatus.FAILED or mission.execution.state in {
        ExecutionState.FAILED,
        ExecutionState.EXHAUSTED,
    }:
        return ExperienceOutcome.FAILURE
    if _finished_successfully(mission) and success >= 0.70:
        return ExperienceOutcome.SUCCESS
    if _finished_successfully(mission):
        return ExperienceOutcome.PARTIAL
    return ExperienceOutcome.FAILURE


def _finished_successfully(mission: Mission) -> bool:
    """Supervisor tests often finish without setting MissionStatus.COMPLETED."""
    if mission.status == MissionStatus.FAILED:
        return False
    if mission.execution.state in {ExecutionState.FAILED, ExecutionState.EXHAUSTED}:
        return False
    if mission.status == MissionStatus.COMPLETED:
        return True
    if mission.investigation_report is not None:
        return True
    plan = mission.delegation_plan
    if plan is not None and plan.status == "COMPLETED":
        return True
    return False


def _tools_used(mission: Mission) -> list[str]:
    names: list[str] = []
    if mission.delegation_plan:
        for task in mission.delegation_plan.tasks:
            if task.capability and task.capability not in names:
                names.append(task.capability)
    for item in mission.tool_invocations:
        if item.tool_name and item.tool_name not in names:
            names.append(item.tool_name)
    return names


def _specialists_used(mission: Mission) -> list[str]:
    if mission.delegation_plan is None:
        return []
    names: list[str] = []
    for task in mission.delegation_plan.tasks:
        if task.agent_id and task.agent_id not in names:
            names.append(task.agent_id)
    return names


def _actions_used(mission: Mission) -> list[str]:
    names: list[str] = []
    for item in mission.actions:
        if item.action_type and item.action_type not in names:
            names.append(item.action_type)
    return names


def _external_used(mission: Mission) -> list[str]:
    names: list[str] = []
    for item in mission.external_invocations:
        if item.tool_name and item.tool_name not in names:
            names.append(item.tool_name)
    return names


def _failed_tasks(mission: Mission) -> int:
    if mission.delegation_plan is None:
        return 0
    return sum(1 for item in mission.delegation_plan.tasks if item.status == StepStatus.FAILED)


def _failed_actions(mission: Mission) -> int:
    return sum(
        1
        for item in mission.actions
        if item.status in {ActionStatus.FAILED, ActionStatus.VERIFICATION_FAILED}
    )


def _failure_count(mission: Mission) -> int:
    failed_tools = sum(
        1 for item in mission.tool_invocations if item.status == StepStatus.FAILED
    )
    failed_external = sum(
        1
        for item in mission.external_invocations
        if item.status.value in {"FAILED", "REJECTED"}
    )
    rejected = sum(1 for item in mission.reasoning_trace if not item.accepted)
    return failed_tools + failed_external + _failed_tasks(mission) + _failed_actions(mission) + rejected


def _repeated_count(mission: Mission) -> int:
    if len(mission.reasoning_trace) < 2:
        return 0
    last = mission.reasoning_trace[-1].fingerprint
    count = 0
    for record in reversed(mission.reasoning_trace):
        if record.fingerprint and record.fingerprint == last:
            count += 1
        else:
            break
    return max(0, count - 1)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
