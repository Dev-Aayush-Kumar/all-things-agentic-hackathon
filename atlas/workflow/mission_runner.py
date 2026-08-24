"""Mission workflow orchestration."""

import asyncio
import logging
from contextvars import ContextVar

from atlas.agent.base import MissionPlanner
from atlas.agent.factory import resolve_initial_tools
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.agent.tools import ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import EventType, ExecutionState, MissionStatus, StepStatus
from atlas.domain.exceptions import DatasetParseError, StaleExecutionError, WaitingForApproval
from atlas.domain.models import Mission, MissionEvent, WorkingCopyState, utc_now
from atlas.execution.context import ExecutionContext
from atlas.investigation.parser import parse_csv_bytes
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository
from atlas.storage.base import DatasetStorage
from atlas.workflow.step_executor import StepExecutor

logger = logging.getLogger(__name__)

_execution_context: ContextVar[ExecutionContext | None] = ContextVar(
    "atlas_execution_context", default=None
)


class MissionWorkflowRunner:
    """Runs the full mission lifecycle in the background."""

    def __init__(
        self,
        repository: MissionRepository,
        planner: MissionPlanner,
        step_executor: StepExecutor,
        dataset_repository: DatasetRepository | None = None,
        dataset_storage: DatasetStorage | None = None,
        reasoner: InvestigationReasoner | None = None,
        settings: Settings | None = None,
        step_delay_seconds: float = 0.0,
        memory_repository=None,
        experience_repository=None,
        strategy_repository=None,
        approval_repository=None,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._step_executor = step_executor
        self._dataset_repository = dataset_repository
        self._dataset_storage = dataset_storage
        self._reasoner = reasoner
        self._settings = settings
        self._step_delay_seconds = step_delay_seconds
        self._memory_repository = memory_repository
        self._experience_repository = experience_repository
        self._strategy_repository = strategy_repository
        self._approval_repository = approval_repository

    async def run(
        self,
        mission_id: str,
        context: ExecutionContext | None = None,
    ) -> None:
        """Execute planning and step execution for a mission."""
        token = _execution_context.set(context)
        try:
            mission = await self._repository.get(mission_id)
            if mission is None:
                logger.error("Mission %s not found for workflow execution", mission_id)
                return

            try:
                await self._plan(mission)
                paused = await self._execute(mission)
                if paused:
                    return
            except StaleExecutionError:
                logger.warning("Lost execution ownership for mission %s", mission_id)
                return
            except Exception as exc:
                logger.exception("Mission %s failed", mission_id)
                try:
                    await self._fail_mission(mission, str(exc))
                except StaleExecutionError:
                    logger.warning(
                        "Lost execution ownership while failing mission %s", mission_id
                    )
        finally:
            _execution_context.reset(token)

    async def _plan(self, mission: Mission) -> None:
        mission.status = MissionStatus.PLANNING
        mission.touch()
        self._add_event(
            mission,
            EventType.PLANNING_STARTED,
            "Planning started",
            {"planner": self._planner.source_name},
        )
        await self._persist(mission)

        plan = await self._planner.create_plan(mission.goal, mission.dataset_id)
        mission.execution_plan = plan
        self._add_event(
            mission,
            EventType.EXECUTION_PLAN_GENERATED,
            "Execution plan generated",
            {
                "planner_source": plan.planner_source.value,
                "step_count": len(plan.steps),
            },
        )
        mission.touch()
        await self._persist(mission)

    async def _execute(self, mission: Mission) -> bool:
        if mission.execution_plan is None:
            raise RuntimeError("Cannot execute mission without an execution plan")

        mission.status = MissionStatus.EXECUTING
        mission.touch()
        self._add_event(mission, EventType.EXECUTION_STARTED, "Execution started")
        await self._persist(mission)

        try:
            if mission.dataset_id:
                paused = await self._execute_investigation(mission)
                if paused:
                    return True
            else:
                await self._execute_generic_steps(mission)
        except WaitingForApproval:
            return True

        mission.status = MissionStatus.COMPLETED
        mission.completed_at = utc_now()
        mission.execution.state = ExecutionState.COMPLETED
        mission.execution.lease_expires_at = None
        mission.touch()
        self._add_event(mission, EventType.MISSION_COMPLETED, "Mission completed")
        await self._persist(mission)
        return False

    async def _execute_generic_steps(self, mission: Mission) -> None:
        assert mission.execution_plan is not None
        for step in mission.execution_plan.steps:
            self._add_event(
                mission,
                EventType.STEP_STARTED,
                f"Step started: {step.title}",
                {"step_id": step.id},
            )
            mission.touch()
            await self._persist(mission)

            updated_step = await self._step_executor.execute(step, mission.goal)
            step.status = updated_step.status
            step.result = updated_step.result
            step.error = updated_step.error

            if step.status == StepStatus.FAILED:
                self._add_event(
                    mission,
                    EventType.STEP_FAILED,
                    f"Step failed: {step.title}",
                    {"step_id": step.id, "error": step.error},
                )
                mission.touch()
                await self._persist(mission)
                raise RuntimeError(step.error or f"Step {step.id} failed")

            self._add_event(
                mission,
                EventType.STEP_COMPLETED,
                f"Step completed: {step.title}",
                {"step_id": step.id},
            )
            mission.touch()
            await self._persist(mission)

    async def _execute_investigation(self, mission: Mission) -> bool:
        if self._dataset_repository is None or self._dataset_storage is None:
            raise RuntimeError("Dataset investigation dependencies are not configured")
        if self._reasoner is None or self._settings is None:
            raise RuntimeError("Agent loop dependencies are not configured")

        dataset = await self._dataset_repository.get(mission.dataset_id or "")
        if dataset is None:
            raise RuntimeError(f"Dataset '{mission.dataset_id}' was not found")

        if mission.working_copy is None:
            mission.working_copy = WorkingCopyState(
                source_dataset_id=dataset.dataset_id,
                source_stored_filename=dataset.stored_filename,
                source_original_filename=dataset.original_filename,
            )
        load_name = (
            mission.working_copy.current_filename()
            if mission.working_copy.current_version > 0
            else dataset.stored_filename
        )
        try:
            content = await self._dataset_storage.load(load_name)
            frame = await asyncio.to_thread(parse_csv_bytes, content)
        except DatasetParseError:
            raise
        except FileNotFoundError as exc:
            raise DatasetParseError(str(exc)) from exc
        except Exception as exc:
            raise DatasetParseError(f"Failed to load dataset: {exc}") from exc

        self._add_event(
            mission,
            EventType.INVESTIGATION_STARTED,
            "Dataset investigation started",
            {
                "dataset_id": dataset.dataset_id,
                "original_filename": dataset.original_filename,
            },
        )
        await self._persist(mission)

        from atlas.agent.factory import create_decision_maker
        from atlas.ops.supervisor import Supervisor

        decision_maker = create_decision_maker(self._settings)
        if decision_maker.drives_initial_plan:
            selected_tools: list[str] | None = None
            plan_source = decision_maker.source
        else:
            selected_tools, plan_source = await resolve_initial_tools(
                mission.goal, self._settings
            )

        memory_retriever = None
        if self._memory_repository is not None and self._settings.memory_enabled:
            from atlas.ops.memory.retrieve import MemoryRetriever

            memory_retriever = MemoryRetriever(self._memory_repository, self._settings)

        strategy_retriever = None
        if self._strategy_repository is not None and self._settings.strategy_enabled:
            from atlas.ops.learning.retrieve import StrategyRetriever

            strategy_retriever = StrategyRetriever(self._strategy_repository, self._settings)

        supervisor = Supervisor(
            reasoner=self._reasoner,
            settings=self._settings,
            plan_source=plan_source,
            selected_tools=selected_tools,
            step_delay_seconds=self._step_delay_seconds,
            dataset_storage=self._dataset_storage,
            decision_maker=decision_maker,
            memory_retriever=memory_retriever,
            strategy_retriever=strategy_retriever,
            approval_repository=self._approval_repository,
        )
        context = ToolContext(
            dataset_id=dataset.dataset_id,
            original_filename=dataset.original_filename,
            frame=frame,
        )

        async def persist() -> None:
            mission.touch()
            await self._persist(mission)

        try:
            await supervisor.run(mission, context, persist)
        except WaitingForApproval:
            return True
        await self._extract_memory(mission)
        await self._record_learning(mission)
        return False

    async def _fail_mission(self, mission: Mission, error: str) -> None:
        mission.status = MissionStatus.FAILED
        mission.error = error
        mission.completed_at = utc_now()
        mission.execution.state = ExecutionState.FAILED
        mission.execution.last_error = error
        mission.execution.lease_expires_at = None
        mission.touch()
        self._add_event(
            mission,
            EventType.ATTEMPT_FAILED,
            "Execution attempt failed",
            {
                "error": error,
                "attempt_count": mission.execution.attempt_count,
            },
        )
        self._add_event(
            mission,
            EventType.MISSION_FAILED,
            "Mission failed",
            {"error": error},
        )
        await self._persist(mission)
        await self._record_learning(mission)

    async def _extract_memory(self, mission: Mission) -> None:
        if self._memory_repository is None or self._settings is None:
            return
        if not self._settings.memory_enabled:
            return
        self._add_event(
            mission,
            EventType.MEMORY_EXTRACTION_STARTED,
            "Post-completion memory extraction started",
            {},
        )
        try:
            from atlas.agent.factory import create_memory_extractor
            from atlas.ops.memory.extract import extract_and_store

            extractor = create_memory_extractor(self._settings)
            stored = await extract_and_store(
                mission,
                self._memory_repository,
                self._settings,
                extractor=extractor,
            )
            logger.info(
                "Memory extraction stored %s record(s) for mission %s",
                len(stored),
                mission.mission_id,
            )
        except Exception as exc:
            logger.exception(
                "Memory extraction failed for mission %s", mission.mission_id
            )
            self._add_event(
                mission,
                EventType.MEMORY_EXTRACTION_FAILED,
                "Memory extraction failed",
                {"error": str(exc)},
            )

    async def _record_learning(self, mission: Mission) -> None:
        if self._experience_repository is None or self._strategy_repository is None:
            return
        if self._settings is None or not self._settings.strategy_enabled:
            return
        try:
            from atlas.ops.learning.extract import record_experience_and_strategy

            await record_experience_and_strategy(
                mission,
                self._experience_repository,
                self._strategy_repository,
                self._settings,
            )
        except Exception as exc:
            logger.exception(
                "Strategy learning failed for mission %s", mission.mission_id
            )
            self._add_event(
                mission,
                EventType.STRATEGY_EXTRACTION_FAILED,
                "Strategy learning failed",
                {"error": str(exc)},
            )
        try:
            await self._persist(mission)
        except Exception:
            logger.exception(
                "Persisting learning events failed for mission %s", mission.mission_id
            )

    async def _persist(self, mission: Mission) -> None:
        context = _execution_context.get()
        if context is None:
            await self._repository.update(mission)
            return
        await self._repository.update_owned(mission, context)

    @staticmethod
    def _add_event(
        mission: Mission,
        event_type: EventType,
        message: str,
        metadata: dict | None = None,
    ) -> None:
        mission.events.append(
            MissionEvent(
                type=event_type,
                message=message,
                metadata=metadata or {},
            )
        )
