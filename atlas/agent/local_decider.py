"""Deterministic decision-maker. Always labeled LOCAL_FALLBACK."""

from __future__ import annotations

from typing import Any

from atlas.agent.policy import select_tools
from atlas.agent.tools import INSPECT_COLUMN, PROFILE_DATASET
from atlas.domain.enums import ModelDecisionKind, PlannerSource
from atlas.domain.models import (
    ModelDecision,
    ProposedActionRequest,
    ProposedExternalRequest,
    ProposedObservation,
    ProposedTask,
)
from atlas.ops.actions.policy import propose_action_follow_ups
from atlas.ops.external.policy import propose_external_follow_up
from atlas.ops.planning import (
    observe_follow_ups,
    synthesis_follow_up,
    task_exists,
)
from atlas.ops.registry import CAPABILITY_SYNTHESIZE
from atlas.ops.workspace import MissionWorkspace


class LocalDecisionMaker:
    """Maps existing evidence policy onto the typed decision contract."""

    def __init__(self, workspace_provider=None) -> None:
        self._workspace_provider = workspace_provider

    @property
    def source(self) -> PlannerSource:
        return PlannerSource.LOCAL_FALLBACK

    @property
    def drives_initial_plan(self) -> bool:
        return False

    async def decide(self, context: dict[str, Any]) -> ModelDecision:
        workspace = context.get("_workspace")
        if not isinstance(workspace, MissionWorkspace):
            if self._workspace_provider is not None:
                workspace = self._workspace_provider()
        if not isinstance(workspace, MissionWorkspace):
            return ModelDecision(
                decision=ModelDecisionKind.COMPLETE,
                reason="Local fallback has no workspace to inspect",
                summary="No workspace",
            )
        return self.decide_from_workspace(workspace)

    def decide_from_workspace(self, workspace: MissionWorkspace) -> ModelDecision:
        mission = workspace.mission
        plan = mission.delegation_plan
        if plan is None or not plan.tasks:
            tools = list(select_tools(mission.goal))
            if PROFILE_DATASET not in tools:
                tools = [PROFILE_DATASET, *tools]
            return ModelDecision(
                decision=ModelDecisionKind.DELEGATE,
                reason="Initial analyst measurements selected from the mission goal",
                tasks=[
                    ProposedTask(
                        capability=tool,
                        objective=f"Measure {tool}",
                    )
                    for tool in tools
                ],
            )

        follow_ups = [
            item
            for item in observe_follow_ups(workspace)
            if not task_exists(plan, item.capability, item.arguments)
        ]
        if len(follow_ups) == 1 and follow_ups[0].capability == INSPECT_COLUMN:
            item = follow_ups[0]
            return ModelDecision(
                decision=ModelDecisionKind.OBSERVE,
                reason=item.reason or item.objective,
                tool=ProposedObservation(
                    name=item.capability,
                    arguments=dict(item.arguments),
                ),
            )
        if follow_ups:
            return ModelDecision(
                decision=ModelDecisionKind.DELEGATE,
                reason="Additional specialist work is justified by observed evidence",
                tasks=[
                    ProposedTask(
                        capability=item.capability,
                        objective=item.objective,
                        inputs=dict(item.arguments),
                    )
                    for item in follow_ups
                ],
            )

        external = propose_external_follow_up(workspace)
        if external:
            return ModelDecision(
                decision=ModelDecisionKind.EXTERNAL,
                reason=external.reason or external.objective,
                external=ProposedExternalRequest(
                    capability=external.capability,
                    arguments=dict(external.arguments),
                ),
            )

        actions = propose_action_follow_ups(workspace)
        if actions:
            item = actions[0]
            action_type = item.arguments.get("action_type")
            parameters = item.arguments.get("parameters") or {}
            return ModelDecision(
                decision=ModelDecisionKind.ACTION,
                reason=item.reason or item.objective,
                action=ProposedActionRequest(
                    type=str(action_type),
                    parameters=dict(parameters) if isinstance(parameters, dict) else {},
                ),
            )

        if mission.dataset_profile is not None and not task_exists(plan, CAPABILITY_SYNTHESIZE):
            reporter = synthesis_follow_up()
            return ModelDecision(
                decision=ModelDecisionKind.DELEGATE,
                reason=reporter.reason,
                tasks=[
                    ProposedTask(
                        capability=reporter.capability,
                        objective=reporter.objective,
                        inputs=dict(reporter.arguments),
                    )
                ],
            )

        return ModelDecision(
            decision=ModelDecisionKind.COMPLETE,
            reason="Local fallback found no further justified work",
            summary="Mission objective can be answered from current evidence",
        )
