"""Mission supervisor / orchestrator.

Owns the mission-level decision loop: understand, delegate, observe, replan, synthesize.
"""

from __future__ import annotations

import asyncio
import logging
import time

from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.policy import understand_goal
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.agent.tools import INVESTIGATION_TOOLS, ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import (
    ActionStatus,
    AgentPhase,
    ApprovalStatus,
    EventType,
    GovernanceVerdict,
    ModelDecisionKind,
    PlannerSource,
    StepStatus,
)
from atlas.domain.exceptions import ModelDecisionError, WaitingForApproval
from atlas.domain.models import (
    ActionRecord,
    AgentPlan,
    DecisionRecord,
    DelegationPlan,
    Mission,
    MissionEvent,
    WorkingCopyState,
)
from atlas.ops.actions.registry import (
    ACTION_CAPABILITIES,
    make_idempotency_key,
)
from atlas.ops.delegation import LocalDelegationManager
from atlas.ops.decisions import validate_decision
from atlas.ops.planning import (
    append_follow_up,
    build_initial_delegation,
    initial_analyst_tools,
    ready_tasks,
    synthesis_follow_up,
    task_exists,
)
from atlas.ops.reasoning_context import build_reasoning_context
from atlas.storage.base import DatasetStorage
from atlas.ops.registry import CAPABILITY_SYNTHESIZE, AgentRegistry, default_registry
from atlas.ops.specialists import build_specialists
from atlas.ops.workspace import MissionWorkspace, PersistFn

logger = logging.getLogger(__name__)


class CriticalTaskFailedError(RuntimeError):
    """Raised when a critical specialist cannot complete its objective."""


class Supervisor:
    """Delegates work to specialists and replans from observed evidence."""

    def __init__(
        self,
        *,
        reasoner: InvestigationReasoner,
        settings: Settings,
        plan_source: PlannerSource,
        selected_tools: list[str] | None = None,
        registry: AgentRegistry | None = None,
        step_delay_seconds: float = 0.0,
        specialists: dict | None = None,
        dataset_storage: DatasetStorage | None = None,
        decision_maker=None,
        external_executor=None,
        memory_retriever=None,
        strategy_retriever=None,
        governance_policy=None,
        approval_repository=None,
    ) -> None:
        self._reasoner = reasoner
        self._settings = settings
        self._plan_source = plan_source
        self._selected_tools = selected_tools
        self._registry = registry or default_registry()
        self._step_delay_seconds = step_delay_seconds
        self._specialists = specialists or build_specialists(self._registry)
        self._delegation = LocalDelegationManager(self._specialists)
        self._dataset_storage = dataset_storage
        self._decision_maker = decision_maker or LocalDecisionMaker()
        self._local_fallback_decider = LocalDecisionMaker()
        self._external_executor = external_executor
        self._memory_retriever = memory_retriever
        self._strategy_retriever = strategy_retriever
        self._governance_policy = governance_policy
        if self._governance_policy is None:
            from atlas.ops.governance.policy import default_governance_policy

            self._governance_policy = default_governance_policy()
        self._approval_repository = approval_repository

    async def run(
        self,
        mission: Mission,
        context: ToolContext,
        persist: PersistFn,
    ) -> None:
        started = time.monotonic()
        workspace = MissionWorkspace(
            mission=mission,
            tool_context=context,
            persist=persist,
            lock=asyncio.Lock(),
            settings=self._settings,
            reasoner=self._reasoner,
            registry=self._registry,
            plan_source=self._plan_source,
            step_delay_seconds=self._step_delay_seconds,
            dataset_storage=self._dataset_storage,
        )
        self._restore_inspected(workspace)
        self._ensure_working_copy(workspace)

        mission.current_phase = AgentPhase.UNDERSTANDING
        understanding = understand_goal(mission.goal)
        mission.current_objective = understanding
        if not any(event.type == EventType.MISSION_UNDERSTOOD for event in mission.events):
            _add_event(
                mission,
                EventType.MISSION_UNDERSTOOD,
                "Mission understood",
                {"objective": understanding, "source": self._plan_source.value},
            )
            await persist()

        resuming = self._prepare_plan(mission)
        if not resuming:
            mission.current_phase = AgentPhase.PLANNING
            if self._decision_maker.drives_initial_plan:
                self._init_empty_plan(mission)
            else:
                tools = initial_analyst_tools(mission.goal, self._selected_tools)
                mission.delegation_plan = build_initial_delegation(
                    mission,
                    tools=tools,
                    source=self._plan_source,
                    registry=self._registry,
                    max_attempts=self._settings.specialist_task_max_attempts,
                )
                if mission.agent_plan is not None:
                    mission.agent_plan.max_iterations = self._settings.agent_max_iterations
                _add_event(
                    mission,
                    EventType.DELEGATION_PLAN_CREATED,
                    "Delegation plan created",
                    {
                        "task_count": len(mission.delegation_plan.tasks),
                        "source": self._plan_source.value,
                        "agent_ids": sorted(
                            {task.agent_id for task in mission.delegation_plan.tasks}
                        ),
                    },
                )
                _add_event(
                    mission,
                    EventType.AGENT_PLAN_CREATED,
                    "Agent plan created",
                    {
                        "selected_tools": tools,
                        "task_count": len(tools),
                        "source": self._plan_source.value,
                    },
                )
                _add_event(
                    mission,
                    EventType.AGENT_DECISION,
                    "Initial capabilities selected from the mission goal",
                    {"selected_tools": tools},
                )
            await persist()
        else:
            logger.info("Resuming mission %s from persisted specialist tasks", mission.mission_id)

        while True:
            plan = mission.delegation_plan
            assert plan is not None
            plan.wave += 1
            if plan.wave > self._settings.agent_max_iterations:
                self._hit_limit(mission, "Supervisor iteration limit reached")
                break
            if time.monotonic() - started > self._settings.agent_max_runtime_seconds:
                self._hit_limit(mission, "Supervisor runtime limit reached")
                break
            if (
                mission.agent_plan is not None
                and mission.agent_plan.tool_call_count >= self._settings.agent_max_tool_calls
                and ready_tasks(plan)
            ):
                self._hit_limit(mission, "Agent tool-call limit reached")
                break
            if mission.model_call_count >= self._settings.max_model_calls and not ready_tasks(plan):
                self._hit_limit(mission, "Model-call limit reached")
                break

            ready = ready_tasks(plan)
            if not ready:
                resumed = await self._resume_resolved_approval(workspace)
                if resumed is True:
                    await persist()
                    continue
                should_continue = await self._consult_decision_maker(workspace)
                if should_continue:
                    await persist()
                    continue
                break
            if any(task.capability in ACTION_CAPABILITIES for task in ready):
                mission.current_phase = AgentPhase.ACTING
            else:
                mission.current_phase = AgentPhase.DELEGATING
            plan.current_task_ids = [task.task_id for task in ready]
            await persist()
            await self._delegation.execute_ready(ready, workspace)
            self._raise_if_critical_exhausted(plan)
            mission.current_phase = AgentPhase.OBSERVING
            _add_event(
                mission,
                EventType.SUPERVISOR_OBSERVED,
                "Supervisor observed specialist results",
                {
                    "wave": plan.wave,
                    "completed": [
                        task.task_id
                        for task in plan.tasks
                        if task.status == StepStatus.COMPLETED
                    ],
                },
            )
            await persist()
            continue

        await self._ensure_report(workspace)

        if mission.dataset_profile is None:
            raise RuntimeError("Supervisor finished without a dataset profile")

        plan = mission.delegation_plan
        assert plan is not None
        if any(
            task.capability == CAPABILITY_SYNTHESIZE and task.status == StepStatus.COMPLETED
            for task in plan.tasks
        ):
            _add_event(
                mission,
                EventType.SYNTHESIS_COMPLETED,
                "Synthesis completed",
                {"reasoning_source": (
                    mission.investigation_report.reasoning_source.value
                    if mission.investigation_report
                    else self._plan_source.value
                )},
            )
            _add_event(
                mission,
                EventType.FINAL_REASONING_COMPLETED,
                "Final reasoning completed",
                {
                    "reasoning_source": (
                        mission.investigation_report.reasoning_source.value
                        if mission.investigation_report
                        else self._plan_source.value
                    )
                },
            )
            _add_event(
                mission,
                EventType.FINAL_REPORT_GENERATED,
                "Final report generated",
                {
                    "finding_count": len(mission.findings),
                    "reasoning_source": (
                        mission.investigation_report.reasoning_source.value
                        if mission.investigation_report
                        else self._plan_source.value
                    ),
                    "interpretation_count": len(mission.interpretations),
                },
            )
            _add_event(
                mission,
                EventType.FINDINGS_PRIORITIZED,
                "Findings prioritized",
                {"finding_count": len(mission.findings)},
            )

        if mission.agent_plan is not None:
            if mission.agent_plan.status != "LIMIT_REACHED":
                mission.agent_plan.status = "COMPLETED"
            mission.agent_plan.current_task_id = None
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        plan.status = "COMPLETED"
        plan.current_task_ids = []
        mission.current_phase = AgentPhase.COMPLETING
        mission.current_task = None
        await persist()

    def _init_empty_plan(self, mission: Mission) -> None:
        objective = understand_goal(mission.goal)
        mission.delegation_plan = DelegationPlan(
            objective=objective,
            source=self._plan_source,
            tasks=[],
        )
        mission.agent_plan = AgentPlan(
            objective=objective,
            source=self._plan_source,
            selected_tools=[],
            tasks=[],
            status="IN_PROGRESS",
            max_iterations=self._settings.agent_max_iterations,
        )
        mission.execution_plan = mission.agent_plan.to_execution_plan()
        mission.current_objective = objective
        _add_event(
            mission,
            EventType.DELEGATION_PLAN_CREATED,
            "Delegation plan created",
            {
                "task_count": 0,
                "source": self._plan_source.value,
                "agent_ids": [],
                "model_driven": True,
            },
        )

    async def _consult_decision_maker(self, workspace: MissionWorkspace) -> bool:
        mission = workspace.mission
        plan = mission.delegation_plan
        assert plan is not None
        mission.current_phase = AgentPhase.REASONING
        mission.reasoning_iteration += 1
        await self._refresh_memories(workspace)
        await self._refresh_strategies(workspace)
        context = build_reasoning_context(workspace)
        context["_workspace"] = workspace
        _add_event(
            mission,
            EventType.MODEL_REASONING_STARTED,
            "Supervisor requested the next typed decision",
            {
                "iteration": mission.reasoning_iteration,
                "source": self._decision_maker.source.value,
            },
        )
        source = self._decision_maker.source
        decision = None
        try:
            if source == PlannerSource.GEMINI_ADK:
                mission.model_call_count += 1
            decision = await self._decision_maker.decide(context)
        except Exception as exc:
            logger.exception("Decision-maker failed mission=%s", mission.mission_id)
            self._store_decision(
                mission,
                source=source,
                decision=None,
                accepted=False,
                rejection_reason=str(exc),
                fingerprint="",
            )
            _add_event(
                mission,
                EventType.MODEL_DECISION_REJECTED,
                "Model decision failed",
                {"error": str(exc), "source": source.value},
            )
            if source == PlannerSource.GEMINI_ADK:
                decision = self._local_fallback_decider.decide_from_workspace(workspace)
                source = PlannerSource.LOCAL_FALLBACK
            else:
                return False
        assert decision is not None
        _add_event(
            mission,
            EventType.MODEL_DECISION_RECEIVED,
            f"Decision received: {decision.decision.value}",
            {
                "decision": decision.decision.value,
                "reason": decision.reason,
                "source": source.value,
            },
        )
        try:
            validated = validate_decision(decision, workspace, registry=self._registry)
        except ModelDecisionError as exc:
            fingerprint = ""
            try:
                from atlas.ops.decisions import decision_fingerprint

                fingerprint = decision_fingerprint(decision)
            except Exception:
                fingerprint = ""
            self._store_decision(
                mission,
                source=source,
                decision=decision,
                accepted=False,
                rejection_reason=str(exc),
                fingerprint=fingerprint,
            )
            _add_event(
                mission,
                EventType.MODEL_DECISION_REJECTED,
                "Model decision rejected",
                {"error": str(exc), "source": source.value},
            )
            if self._repeated_limit_reached(mission, fingerprint):
                self._hit_limit(mission, "Repeated identical decisions are bounded")
                return False
            return True

        _add_event(
            mission,
            EventType.MODEL_DECISION_VALIDATED,
            f"Decision validated: {validated.decision.decision.value}",
            {
                "decision": validated.decision.decision.value,
                "source": source.value,
            },
        )
        from atlas.ops.learning.influence import note_strategy_influence

        note_strategy_influence(workspace, validated.decision)

        allowed = await self._govern_validated(workspace, validated, source)
        if allowed == "stop":
            return False
        if allowed == "replan":
            return True
        if allowed == "done":
            return True
        if self._repeated_limit_reached(mission, validated.fingerprint):
            self._hit_limit(mission, "Repeated identical decisions are bounded")
            return False
        return await self._enact_validated(workspace, validated, source)

    async def _govern_validated(self, workspace, validated, source) -> str:
        """ATLAS policy. The model cannot approve itself. Returns False to skip execution."""
        from atlas.ops.governance.events import append_governance_event
        from atlas.ops.governance.lifecycle import (
            apply_waiting_state,
            persist_or_reuse_approval,
        )
        from atlas.ops.governance.policy import GovernanceDecision

        mission = workspace.mission
        governance = self._governance_policy.evaluate(validated, workspace)
        if (
            not workspace.settings.governance_enabled
            and governance.verdict == GovernanceVerdict.REQUIRE_APPROVAL
        ):
            governance = GovernanceDecision(
                verdict=GovernanceVerdict.AUTO_APPROVE,
                risk=governance.risk,
                reason="Governance disabled; validated operation auto-approved",
                operation_kind=governance.operation_kind,
                capability=governance.capability,
                parameters=governance.parameters,
                fingerprint=governance.fingerprint,
                requested_operation=governance.requested_operation,
            )
        append_governance_event(
            mission,
            verdict=governance.verdict,
            risk=governance.risk,
            reason=governance.reason,
            fingerprint=governance.fingerprint,
            event_type=EventType.GOVERNANCE_EVALUATED,
        )
        if governance.verdict == GovernanceVerdict.DENY:
            self._store_decision(
                mission,
                source=source,
                decision=validated.decision,
                accepted=False,
                rejection_reason=governance.reason,
                fingerprint=validated.fingerprint,
            )
            append_governance_event(
                mission,
                verdict=governance.verdict,
                risk=governance.risk,
                reason=governance.reason,
                fingerprint=governance.fingerprint,
                event_type=EventType.GOVERNANCE_DENIED,
            )
            if self._repeated_limit_reached(mission, validated.fingerprint):
                self._hit_limit(mission, "Repeated identical decisions are bounded")
                return "stop"
            return "replan"
        self._store_decision(
            mission,
            source=source,
            decision=validated.decision,
            accepted=True,
            rejection_reason=None,
            fingerprint=validated.fingerprint,
        )
        if governance.verdict == GovernanceVerdict.REQUIRE_APPROVAL:
            if self._approval_repository is None:
                return "execute"
            decision_id = (
                mission.reasoning_trace[-1].decision_id if mission.reasoning_trace else None
            )
            record = await persist_or_reuse_approval(
                self._approval_repository,
                mission,
                validated,
                governance,
                workspace.settings,
                decision_id=decision_id,
            )
            if record.status == ApprovalStatus.APPROVED:
                enacted = await self._consume_approved(workspace, record)
                return "done" if enacted else "replan"
            apply_waiting_state(mission, record)
            await workspace.persist()
            raise WaitingForApproval(record.approval_id, mission.mission_id)
        return "execute"

    async def _resume_resolved_approval(self, workspace) -> bool | None:
        if self._approval_repository is None:
            return None
        mission = workspace.mission
        if not mission.pending_approval_id:
            return None
        from atlas.ops.governance.events import append_governance_event
        from atlas.ops.governance.lifecycle import (
            apply_waiting_state,
            maybe_expire,
            record_rejected_action,
        )

        record = await self._approval_repository.get(mission.pending_approval_id)
        if record is None:
            mission.pending_approval_id = None
            return None
        previous = record.status
        record = maybe_expire(record)
        if record.status == ApprovalStatus.EXPIRED and previous == ApprovalStatus.PENDING:
            await self._approval_repository.upsert(record)
            append_governance_event(
                mission,
                verdict=GovernanceVerdict.REQUIRE_APPROVAL,
                risk=record.risk,
                reason="Approval request expired",
                fingerprint=record.fingerprint,
                decision_id=record.decision_id,
                approval_id=record.approval_id,
                resolver="system",
                new_status=ApprovalStatus.EXPIRED,
                previous_status=ApprovalStatus.PENDING,
                event_type=EventType.APPROVAL_EXPIRED,
            )
        if record.status == ApprovalStatus.PENDING:
            apply_waiting_state(mission, record)
            await workspace.persist()
            raise WaitingForApproval(record.approval_id, mission.mission_id)
        if record.status == ApprovalStatus.APPROVED:
            return await self._consume_approved(workspace, record)
        if record.status in {
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.CANCELLED,
        }:
            if record.status == ApprovalStatus.REJECTED:
                record_rejected_action(mission, record)
            mission.pending_approval_id = None
            await workspace.persist()
            return False
        if record.status == ApprovalStatus.CONSUMED:
            mission.pending_approval_id = None
            return None
        return None

    async def _consume_approved(self, workspace, record) -> bool:
        from atlas.domain.models import utc_now
        from atlas.ops.decisions import parse_model_decision, validate_decision
        from atlas.ops.governance.events import append_governance_event
        from atlas.ops.governance.lifecycle import approval_fingerprint

        mission = workspace.mission
        try:
            decision = parse_model_decision(record.decision_snapshot)
            validated = validate_decision(decision, workspace, registry=self._registry)
        except ModelDecisionError as exc:
            logger.warning(
                "Approved snapshot failed re-validation mission=%s: %s",
                mission.mission_id,
                exc,
            )
            mission.pending_approval_id = None
            return False
        expected = approval_fingerprint(mission.mission_id, validated.fingerprint)
        if expected != record.fingerprint:
            mission.pending_approval_id = None
            return False
        source = PlannerSource.LOCAL_FALLBACK
        for item in reversed(mission.reasoning_trace):
            if record.decision_id and item.decision_id == record.decision_id:
                source = item.source
                break
        record.status = ApprovalStatus.CONSUMED
        record.consumed_at = utc_now()
        await self._approval_repository.upsert(record)
        mission.pending_approval_id = None
        append_governance_event(
            mission,
            verdict=GovernanceVerdict.REQUIRE_APPROVAL,
            risk=record.risk,
            reason="Approved operation resumed",
            fingerprint=record.fingerprint,
            decision_id=record.decision_id,
            approval_id=record.approval_id,
            resolver=record.resolver,
            resolver_source=record.resolver_source,
            previous_status=ApprovalStatus.APPROVED,
            new_status=ApprovalStatus.CONSUMED,
            event_type=EventType.APPROVAL_RESUMED,
        )
        _add_event(
            mission,
            EventType.APPROVAL_CONSUMED,
            "Approved operation consumed exactly once",
            {"approval_id": record.approval_id, "fingerprint": record.fingerprint},
        )
        return await self._enact_validated(workspace, validated, source)

    async def _enact_validated(self, workspace, validated, source) -> bool:
        mission = workspace.mission
        plan = mission.delegation_plan
        assert plan is not None
        if validated.decision.decision == ModelDecisionKind.COMPLETE:
            _add_event(
                mission,
                EventType.MODEL_COMPLETED,
                "Model completed the mission",
                {"reason": validated.decision.reason, "source": source.value},
            )
            return False

        if validated.decision.decision == ModelDecisionKind.EXTERNAL:
            await self._execute_external_decision(workspace, validated.decision, source)
            return True

        added = self._apply_follow_ups(
            mission,
            validated.follow_ups,
            adaptive=validated.decision.decision != ModelDecisionKind.ACTION,
            proposal_source=source,
        )
        if not added:
            return True
        plan.replan_count += 1
        if validated.decision.decision == ModelDecisionKind.ACTION:
            mission.current_phase = AgentPhase.ACTING
        elif validated.decision.decision == ModelDecisionKind.OBSERVE:
            mission.current_phase = AgentPhase.OBSERVING
        elif any(task.capability == CAPABILITY_SYNTHESIZE for task in added):
            mission.current_phase = AgentPhase.SYNTHESIZING
            _add_event(
                mission,
                EventType.SYNTHESIS_STARTED,
                "Synthesis started",
                {"task_id": added[0].task_id},
            )
        else:
            mission.current_phase = AgentPhase.ADAPTING
        _add_event(
            mission,
            EventType.REPLAN_TRIGGERED,
            "Supervisor replanned after a validated decision",
            {
                "added_task_ids": [task.task_id for task in added],
                "replan_count": plan.replan_count,
            },
        )
        _add_event(
            mission,
            EventType.MODEL_REPLANNED,
            "Model decision added work",
            {
                "capabilities": [task.capability for task in added],
                "source": source.value,
            },
        )
        if any(task.inputs.get("reobserve") for task in added):
            _add_event(
                mission,
                EventType.REPLAN_AFTER_ACTION,
                "Supervisor replanned after a verified action",
                {
                    "added_task_ids": [task.task_id for task in added],
                    "capabilities": [task.capability for task in added],
                },
            )
        _add_event(
            mission,
            EventType.AGENT_DECISION,
            validated.decision.reason,
            {"capabilities": [task.capability for task in added], "source": source.value},
        )
        if validated.decision.decision == ModelDecisionKind.OBSERVE:
            _add_event(
                mission,
                EventType.OBSERVATION_REQUESTED,
                "Observation requested",
                {
                    "tool": validated.decision.tool.name if validated.decision.tool else None,
                    "source": source.value,
                },
            )
        for task in added:
            _add_event(
                mission,
                EventType.SPECIALIST_DELEGATED,
                f"Specialist work accepted: {task.capability}",
                {
                    "task_id": task.task_id,
                    "agent_id": task.agent_id,
                    "capability": task.capability,
                    "source": source.value,
                },
            )
        return True

    async def _refresh_memories(self, workspace: MissionWorkspace) -> None:
        if self._memory_retriever is None or not workspace.settings.memory_enabled:
            return
        from atlas.ops.memory.retrieve import MemoryQuery

        try:
            workspace.retrieved_memories = await self._memory_retriever.retrieve(
                MemoryQuery(
                    goal=workspace.mission.goal,
                    dataset_id=workspace.mission.dataset_id,
                    mission_id=workspace.mission.mission_id,
                    limit=workspace.settings.memory_max_retrieval,
                )
            )
        except Exception:
            logger.exception(
                "Memory retrieval failed mission=%s", workspace.mission.mission_id
            )
            workspace.retrieved_memories = []

    async def _refresh_strategies(self, workspace: MissionWorkspace) -> None:
        if self._strategy_retriever is None or not workspace.settings.strategy_enabled:
            return
        try:
            records = await self._strategy_retriever.retrieve_for_mission(workspace.mission)
            workspace.retrieved_strategies = records
            ids = [item.strategy_id for item in records]
            first = not workspace.mission.strategy_ids_considered and bool(ids)
            workspace.mission.strategy_ids_considered = ids
            if first:
                _add_event(
                    workspace.mission,
                    EventType.STRATEGY_RETRIEVED,
                    "Historical strategies retrieved for reasoning",
                    {"count": len(records), "strategy_ids": ids},
                )
        except Exception:
            logger.exception(
                "Strategy retrieval failed mission=%s", workspace.mission.mission_id
            )
            workspace.retrieved_strategies = []

    async def _execute_external_decision(self, workspace, decision, source) -> None:
        from atlas.ops.external.executor import ExternalToolExecutor

        mission = workspace.mission
        request = decision.external
        assert request is not None
        _add_event(
            mission,
            EventType.EXTERNAL_TOOL_PROPOSED,
            f"External tool proposed: {request.capability}",
            {
                "capability": request.capability,
                "source": source.value,
                "reason": decision.reason,
            },
        )
        executor = self._external_executor or ExternalToolExecutor()
        _add_event(
            mission,
            EventType.EXTERNAL_TOOL_STARTED,
            f"External tool started: {request.capability}",
            {"capability": request.capability, "source": source.value},
        )
        invocation = await executor.invoke(
            workspace,
            capability=request.capability,
            arguments=dict(request.arguments),
            reason=decision.reason,
            source=source,
        )
        if mission.agent_plan is not None:
            mission.agent_plan.tool_call_count += 1
        if invocation.status.value == "AUTHORIZED" or invocation.status.value == "SUCCEEDED":
            _add_event(
                mission,
                EventType.EXTERNAL_TOOL_AUTHORIZED,
                f"External tool authorized: {request.capability}",
                {"capability": request.capability, "invocation_id": invocation.invocation_id},
            )
        if invocation.status.value == "SUCCEEDED":
            _add_event(
                mission,
                EventType.EXTERNAL_TOOL_COMPLETED,
                f"External tool completed: {request.capability}",
                {
                    "capability": request.capability,
                    "evidence_id": invocation.evidence_id,
                    "source_url": invocation.source_url,
                },
            )
            _add_event(
                mission,
                EventType.EVIDENCE_RECEIVED,
                "External evidence recorded",
                {
                    "evidence_id": invocation.evidence_id,
                    "source_type": "EXTERNAL",
                    "tool_name": request.capability,
                },
            )
        elif invocation.status.value == "REJECTED":
            _add_event(
                mission,
                EventType.EXTERNAL_TOOL_REJECTED,
                "External tool rejected",
                {"capability": request.capability, "error": invocation.error},
            )
        else:
            _add_event(
                mission,
                EventType.EXTERNAL_TOOL_FAILED,
                "External tool failed",
                {"capability": request.capability, "error": invocation.error},
            )
        await workspace.persist()

    def _store_decision(
        self,
        mission: Mission,
        *,
        source: PlannerSource,
        decision,
        accepted: bool,
        rejection_reason: str | None,
        fingerprint: str,
    ) -> None:
        evidence_ids = [item.evidence_id for item in mission.evidence_records[-8:]]
        mission.reasoning_trace.append(
            DecisionRecord(
                iteration=mission.reasoning_iteration,
                source=source,
                decision=decision,
                accepted=accepted,
                rejection_reason=rejection_reason,
                evidence_ids=evidence_ids,
                fingerprint=fingerprint,
            )
        )

    def _repeated_limit_reached(self, mission: Mission, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        streak = 0
        for record in reversed(mission.reasoning_trace):
            if record.fingerprint == fingerprint:
                streak += 1
            else:
                break
        return streak >= self._settings.max_repeated_decisions

    def _prepare_plan(self, mission: Mission) -> bool:
        plan = mission.delegation_plan
        if plan is None or not plan.tasks:
            return False
        for task in plan.tasks:
            if task.status == StepStatus.IN_PROGRESS:
                task.status = StepStatus.PENDING
                task.started_at = None
                task.error = "Interrupted; will retry from persisted state"
        if mission.agent_plan is not None:
            for task in mission.agent_plan.tasks:
                if task.status == StepStatus.IN_PROGRESS:
                    task.status = StepStatus.PENDING
        for action in mission.actions:
            if action.status in {ActionStatus.RUNNING, ActionStatus.AUTHORIZED}:
                action.status = ActionStatus.PROPOSED
                action.started_at = None
                action.error = "Interrupted; will retry from last verified working copy"
        return any(task.status == StepStatus.COMPLETED for task in plan.tasks)

    def _apply_follow_ups(
        self,
        mission: Mission,
        follow_ups,
        *,
        adaptive: bool,
        proposal_source: PlannerSource | None = None,
    ) -> list:
        plan = mission.delegation_plan
        assert plan is not None
        added = []
        completed_ids = _completed_ids(plan)
        for follow_up in follow_ups:
            task = append_follow_up(
                mission,
                follow_up,
                registry=self._registry,
                max_attempts=self._settings.specialist_task_max_attempts,
                depends_on=completed_ids,
            )
            if task is None:
                continue
            added.append(task)
            if follow_up.capability in ACTION_CAPABILITIES:
                self._record_proposed_action(
                    mission,
                    task,
                    follow_up,
                    source=proposal_source or PlannerSource.LOCAL_FALLBACK,
                )
            if follow_up.capability in INVESTIGATION_TOOLS:
                _add_event(
                    mission,
                    EventType.ADAPTIVE_INVESTIGATION_TRIGGERED,
                    "Adaptive investigation triggered",
                    {
                        "tool_name": follow_up.capability,
                        "arguments": {
                            key: value
                            for key, value in follow_up.arguments.items()
                            if key != "adaptive"
                        },
                        "reason": follow_up.reason,
                        "agent_id": task.agent_id,
                    },
                )
        return added

    def _raise_if_critical_exhausted(self, plan) -> None:
        for task in plan.tasks:
            if (
                task.critical
                and task.status == StepStatus.FAILED
                and task.attempt_count >= task.max_attempts
            ):
                raise CriticalTaskFailedError(
                    task.error or f"Critical task '{task.capability}' failed"
                )

    def _hit_limit(self, mission: Mission, message: str) -> None:
        if mission.agent_plan is not None:
            for task in mission.agent_plan.tasks:
                if task.status == StepStatus.PENDING:
                    task.status = StepStatus.SKIPPED
                    task.result_summary = "Skipped because an agent loop limit was reached"
            mission.agent_plan.status = "LIMIT_REACHED"
            mission.execution_plan = mission.agent_plan.to_execution_plan()
        if mission.delegation_plan is not None:
            for task in mission.delegation_plan.tasks:
                if task.status == StepStatus.PENDING:
                    task.status = StepStatus.SKIPPED
                    task.error = message
            mission.delegation_plan.status = "LIMIT_REACHED"
        _add_event(
            mission,
            EventType.AGENT_LOOP_LIMIT_REACHED,
            message,
            {
                "max_iterations": self._settings.agent_max_iterations,
                "max_tool_calls": self._settings.agent_max_tool_calls,
            },
        )

    async def _ensure_report(self, workspace: MissionWorkspace) -> None:
        mission = workspace.mission
        if mission.investigation_report is not None or mission.dataset_profile is None:
            return
        plan = mission.delegation_plan
        if plan is None:
            return
        if task_exists(plan, CAPABILITY_SYNTHESIZE):
            synth = next(
                task
                for task in plan.tasks
                if task.capability == CAPABILITY_SYNTHESIZE
            )
            if synth.status == StepStatus.COMPLETED:
                return
            if synth.status in {StepStatus.PENDING, StepStatus.SKIPPED, StepStatus.FAILED}:
                if synth.status == StepStatus.FAILED:
                    return
                synth.status = StepStatus.PENDING
                await self._delegation.execute_ready([synth], workspace)
                return
        reporter = append_follow_up(
            mission,
            synthesis_follow_up(),
            registry=self._registry,
            max_attempts=self._settings.specialist_task_max_attempts,
            depends_on=[],
        )
        if reporter is not None:
            _add_event(
                mission,
                EventType.SYNTHESIS_STARTED,
                "Synthesis started",
                {"task_id": reporter.task_id},
            )
            await self._delegation.execute_ready([reporter], workspace)

    def _record_proposed_action(
        self,
        mission: Mission,
        task,
        follow_up,
        *,
        source: PlannerSource = PlannerSource.LOCAL_FALLBACK,
    ) -> None:
        action_type = follow_up.arguments.get("action_type")
        if not isinstance(action_type, str):
            return
        parameters = follow_up.arguments.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        input_version = (
            mission.working_copy.current_version if mission.working_copy is not None else 0
        )
        key = make_idempotency_key(
            mission_id=mission.mission_id,
            action_type=action_type,
            parameters=parameters,
            input_version=input_version,
        )
        if any(item.idempotency_key == key for item in mission.actions):
            _add_event(
                mission,
                EventType.ACTION_PROPOSED,
                f"Action proposed: {action_type}",
                {
                    "action_type": action_type,
                    "task_id": task.task_id,
                    "source": source.value,
                    "reason": follow_up.reason,
                },
            )
            return
        mission.actions.append(
            ActionRecord(
                mission_id=mission.mission_id,
                task_id=task.task_id,
                agent_id=task.agent_id,
                action_type=action_type,
                objective=follow_up.objective,
                parameters=parameters,
                status=ActionStatus.PROPOSED,
                provenance=source,
                max_attempts=self._settings.action_max_attempts,
                idempotency_key=key,
                input_version=input_version,
            )
        )
        _add_event(
            mission,
            EventType.ACTION_PROPOSED,
            f"Action proposed: {action_type}",
            {
                "action_type": action_type,
                "task_id": task.task_id,
                "source": source.value,
                "reason": follow_up.reason,
            },
        )

    @staticmethod
    def _ensure_working_copy(workspace: MissionWorkspace) -> None:
        mission = workspace.mission
        if mission.working_copy is not None:
            return
        if not workspace.tool_context.dataset_id:
            return
        mission.working_copy = WorkingCopyState(
            source_dataset_id=workspace.tool_context.dataset_id,
            source_stored_filename="",
            source_original_filename=workspace.tool_context.original_filename,
        )

    @staticmethod
    def _restore_inspected(workspace: MissionWorkspace) -> None:
        for record in workspace.mission.evidence_records:
            if record.tool_name != "inspect_column":
                continue
            column = record.observed_facts.get("column_name")
            if isinstance(column, str):
                workspace.inspected_columns.add(column)


def _completed_ids(plan) -> list[str]:
    return [task.task_id for task in plan.tasks if task.status == StepStatus.COMPLETED]


def _add_event(
    mission: Mission,
    event_type: EventType,
    message: str,
    metadata: dict | None = None,
) -> None:
    mission.events.append(
        MissionEvent(type=event_type, message=message, metadata=metadata or {})
    )
    mission.touch()
