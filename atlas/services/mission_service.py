"""Mission business logic."""

from atlas.domain.enums import EventType, ExecutionState, MissionStatus
from atlas.domain.exceptions import (
    CloudDispatchError,
    CloudDispatchNotConfiguredError,
    DatasetNotFoundError,
)
from atlas.domain.models import (
    CreateMissionResponse,
    Mission,
    MissionDetailResponse,
    MissionEvent,
    MissionExecution,
)
from atlas.execution.dispatcher import MissionDispatcher
from atlas.execution.idempotency import mission_fingerprint, normalize_idempotency_key
from atlas.execution.recovery import MissionRecoveryService, RecoveryResult
from atlas.config.settings import Settings
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository


class MissionService:
    """Coordinates mission submission. Long-running work is dispatched to workers."""

    def __init__(
        self,
        repository: MissionRepository,
        dispatcher: MissionDispatcher,
        recovery: MissionRecoveryService,
        settings: Settings,
        dataset_repository: DatasetRepository | None = None,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._recovery = recovery
        self._settings = settings
        self._dataset_repository = dataset_repository

    async def create_mission(
        self,
        goal: str,
        dataset_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CreateMissionResponse:
        """Persist a queued mission and dispatch it. Does not run the workflow inline."""
        if dataset_id:
            if self._dataset_repository is None:
                raise DatasetNotFoundError(dataset_id)
            dataset = await self._dataset_repository.get(dataset_id)
            if dataset is None:
                raise DatasetNotFoundError(dataset_id)

        mission = Mission(
            goal=goal.strip(),
            dataset_id=dataset_id,
            execution=MissionExecution(
                state=ExecutionState.QUEUED,
                max_attempts=self._settings.max_execution_attempts,
            ),
        )
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
        mission.events.append(
            MissionEvent(
                type=EventType.MISSION_QUEUED,
                message="Mission queued for dispatch",
                metadata={"dispatcher": self._dispatcher.backend_name},
            )
        )

        key = normalize_idempotency_key(idempotency_key)
        replayed = False
        if key:
            fingerprint = mission_fingerprint(mission.goal, mission.dataset_id)
            mission, replayed = await self._repository.create_idempotent(
                mission, key, fingerprint
            )
        else:
            await self._repository.create(mission)

        if self._should_dispatch(mission, replayed):
            try:
                await self._dispatcher.dispatch(mission.mission_id)
            except (CloudDispatchError, CloudDispatchNotConfiguredError) as exc:
                raise CloudDispatchError(
                    f"Mission '{mission.mission_id}' was saved but dispatch failed: {exc}"
                ) from exc

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

    async def recover_abandoned(self) -> RecoveryResult:
        """Requeue or exhaust missions with expired leases."""
        return await self._recovery.recover()

    @staticmethod
    def _should_dispatch(mission: Mission, replayed: bool) -> bool:
        if mission.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}:
            return False
        if mission.execution.state in {
            ExecutionState.COMPLETED,
            ExecutionState.FAILED,
            ExecutionState.EXHAUSTED,
        }:
            return False
        if not replayed:
            return True
        return mission.execution.state == ExecutionState.QUEUED and not mission.execution.is_claimed()
