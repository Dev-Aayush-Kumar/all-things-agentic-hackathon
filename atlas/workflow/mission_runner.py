"""Mission workflow orchestration."""

import logging

from atlas.agent.base import MissionPlanner
from atlas.domain.enums import EventType, MissionStatus, StepStatus
from atlas.domain.models import Mission, MissionEvent
from atlas.persistence.base import MissionRepository
from atlas.workflow.step_executor import StepExecutor

logger = logging.getLogger(__name__)


class MissionWorkflowRunner:
    """Runs the full mission lifecycle in the background."""

    def __init__(
        self,
        repository: MissionRepository,
        planner: MissionPlanner,
        step_executor: StepExecutor,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._step_executor = step_executor

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

        plan = await self._planner.create_plan(mission.goal)
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

        mission.status = MissionStatus.COMPLETED
        from atlas.domain.models import utc_now

        mission.completed_at = utc_now()
        mission.touch()
        self._add_event(mission, EventType.MISSION_COMPLETED, "Mission completed")
        await self._repository.update(mission)

    async def _fail_mission(self, mission: Mission, error: str) -> None:
        mission.status = MissionStatus.FAILED
        mission.error = error
        from atlas.domain.models import utc_now

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
