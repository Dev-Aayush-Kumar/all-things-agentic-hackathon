"""Mission business logic."""

from atlas.agent.base import MissionPlanner
from atlas.domain.enums import EventType
from atlas.domain.exceptions import DatasetNotFoundError
from atlas.domain.models import (
    CreateMissionResponse,
    Mission,
    MissionDetailResponse,
    MissionEvent,
)
from atlas.execution.base import BackgroundExecutor
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository
from atlas.workflow.mission_runner import MissionWorkflowRunner


class MissionService:
    """Coordinates mission creation and background workflow execution."""

    def __init__(
        self,
        repository: MissionRepository,
        planner: MissionPlanner,
        background_executor: BackgroundExecutor,
        workflow_runner: MissionWorkflowRunner,
        dataset_repository: DatasetRepository | None = None,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._background_executor = background_executor
        self._workflow_runner = workflow_runner
        self._dataset_repository = dataset_repository

    async def create_mission(
        self,
        goal: str,
        dataset_id: str | None = None,
    ) -> CreateMissionResponse:
        """Create a mission and start background workflow."""
        if dataset_id:
            if self._dataset_repository is None:
                raise DatasetNotFoundError(dataset_id)
            dataset = await self._dataset_repository.get(dataset_id)
            if dataset is None:
                raise DatasetNotFoundError(dataset_id)

        mission = Mission(goal=goal.strip(), dataset_id=dataset_id)
        metadata: dict = {"goal": mission.goal}
        if dataset_id:
            metadata["dataset_id"] = dataset_id
        mission.events.append(
            MissionEvent(
                type=EventType.MISSION_CREATED,
                message="Mission created",
                metadata=metadata,
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
            dataset_id=mission.dataset_id,
        )

    async def get_mission(self, mission_id: str) -> MissionDetailResponse | None:
        """Retrieve mission details."""
        mission = await self._repository.get(mission_id)
        if mission is None:
            return None
        return MissionDetailResponse.from_mission(mission)
