"""Mission workflow orchestration."""

import asyncio
import logging

from atlas.agent.base import MissionPlanner
from atlas.agent.factory import resolve_initial_tools
from atlas.agent.loop import AgentLoop
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.agent.tools import ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import EventType, MissionStatus, StepStatus
from atlas.domain.exceptions import DatasetParseError
from atlas.domain.models import Mission, MissionEvent, utc_now
from atlas.investigation.parser import parse_csv_bytes
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository
from atlas.storage.base import DatasetStorage
from atlas.workflow.step_executor import StepExecutor

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._step_executor = step_executor
        self._dataset_repository = dataset_repository
        self._dataset_storage = dataset_storage
        self._reasoner = reasoner
        self._settings = settings
        self._step_delay_seconds = step_delay_seconds

    async def run(self, mission_id: str) -> None:
        """Execute planning and step execution for a mission."""
        mission = await self._repository.get(mission_id)
        if mission is None:
            logger.error("Mission %s not found for workflow execution", mission_id)
            return

        try:
            await self._plan(mission)
            await self._execute(mission)
        except Exception as exc:
            logger.exception("Mission %s failed", mission_id)
            await self._fail_mission(mission, str(exc))

    async def _plan(self, mission: Mission) -> None:
        mission.status = MissionStatus.PLANNING
        mission.touch()
        self._add_event(
            mission,
            EventType.PLANNING_STARTED,
            "Planning started",
            {"planner": self._planner.source_name},
        )
        await self._repository.update(mission)

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
        await self._repository.update(mission)

    async def _execute(self, mission: Mission) -> None:
        if mission.execution_plan is None:
            raise RuntimeError("Cannot execute mission without an execution plan")

        mission.status = MissionStatus.EXECUTING
        mission.touch()
        self._add_event(mission, EventType.EXECUTION_STARTED, "Execution started")
        await self._repository.update(mission)

        if mission.dataset_id:
            await self._execute_investigation(mission)
        else:
            await self._execute_generic_steps(mission)

        mission.status = MissionStatus.COMPLETED
        mission.completed_at = utc_now()
        mission.touch()
        self._add_event(mission, EventType.MISSION_COMPLETED, "Mission completed")
        await self._repository.update(mission)

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
            await self._repository.update(mission)

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
                await self._repository.update(mission)
                raise RuntimeError(step.error or f"Step {step.id} failed")

            self._add_event(
                mission,
                EventType.STEP_COMPLETED,
                f"Step completed: {step.title}",
                {"step_id": step.id},
            )
            mission.touch()
            await self._repository.update(mission)

    async def _execute_investigation(self, mission: Mission) -> None:
        if self._dataset_repository is None or self._dataset_storage is None:
            raise RuntimeError("Dataset investigation dependencies are not configured")
        if self._reasoner is None or self._settings is None:
            raise RuntimeError("Agent loop dependencies are not configured")

        dataset = await self._dataset_repository.get(mission.dataset_id or "")
        if dataset is None:
            raise RuntimeError(f"Dataset '{mission.dataset_id}' was not found")

        try:
            content = await self._dataset_storage.load(dataset.stored_filename)
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
        await self._repository.update(mission)

        selected_tools, plan_source = await resolve_initial_tools(
            mission.goal, self._settings
        )
        loop = AgentLoop(
            reasoner=self._reasoner,
            settings=self._settings,
            plan_source=plan_source,
            selected_tools=selected_tools,
            step_delay_seconds=self._step_delay_seconds,
        )
        context = ToolContext(
            dataset_id=dataset.dataset_id,
            original_filename=dataset.original_filename,
            frame=frame,
        )

        async def persist() -> None:
            mission.touch()
            await self._repository.update(mission)

        await loop.run(mission, context, persist)

    async def _fail_mission(self, mission: Mission, error: str) -> None:
        mission.status = MissionStatus.FAILED
        mission.error = error
        mission.completed_at = utc_now()
        mission.touch()
        self._add_event(
            mission,
            EventType.MISSION_FAILED,
            "Mission failed",
            {"error": error},
        )
        await self._repository.update(mission)

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
