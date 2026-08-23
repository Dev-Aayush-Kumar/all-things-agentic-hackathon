"""Mission business logic."""

from atlas.agent.base import MissionPlanner
from atlas.domain.enums import EventType, MissionStatus
from atlas.domain.models import (
    CreateMissionResponse,
    Mission,
    MissionDetailResponse,
    MissionEvent,
)
from atlas.execution.base import BackgroundExecutor
from atlas.persistence.base import MissionRepository
from atlas.workflow.mission_runner import MissionWorkflowRunner


class MissionService:
    """Coordinates mission creation and background workflow execution."""

    def __init__(
        self,
        repository: MissionRepository,
        planner: MissionPlanner,
        background_executor: BackgroundExecutor,
        workflow_runner: MissionWorkflowRunner,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._background_executor = background_executor
        self._workflow_runner = workflow_runner

    async def create_mission(self, goal: str) -> CreateMissionResponse:
        """Create a mission and start background workflow."""
        mission = Mission(goal=goal.strip())
        mission.events.append(
            MissionEvent(
                type=EventType.MISSION_CREATED,
                message="Mission created",
                metadata={"goal": mission.goal},
            )
        )
        await self._repository.create(mission)

        self._background_executor.submit(
            lambda: self._workflow_runner.run(mission.mission_id)
        )

        return CreateMissionResponse(
            mission_id=mission.mission_id,
            status=mission.status,
            created_at=mission.created_at,
        )

    async def get_mission(self, mission_id: str) -> MissionDetailResponse | None:
        """Retrieve mission details."""
        mission = await self._repository.get(mission_id)
        if mission is None:
            return None
        return MissionDetailResponse.from_mission(mission)
