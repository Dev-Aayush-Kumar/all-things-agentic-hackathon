"""FastAPI application dependencies."""

from functools import lru_cache

from atlas.agent.factory import create_investigation_reasoner, create_mission_planner
from atlas.config.settings import DispatcherBackend, Settings, get_settings
from atlas.execution.factory import create_dispatcher
from atlas.execution.recovery import MissionRecoveryService
from atlas.execution.worker import MissionWorker
from atlas.persistence.factory import (
    create_dataset_repository,
    create_memory_repository,
    create_mission_repository,
)
from atlas.services.memory_service import MemoryService
from atlas.services.dataset_service import DatasetService
from atlas.services.mission_service import MissionService
from atlas.storage.factory import create_dataset_storage
from atlas.workflow.mission_runner import MissionWorkflowRunner
from atlas.workflow.step_executor import StepExecutor


@lru_cache
def get_app_settings() -> Settings:
    """Cached settings for dependency injection."""
    return get_settings()


def _build_workflow_runner(settings: Settings) -> MissionWorkflowRunner:
    repository = create_mission_repository(settings)
    return MissionWorkflowRunner(
        repository=repository,
        planner=create_mission_planner(settings),
        step_executor=StepExecutor(settings),
        dataset_repository=create_dataset_repository(settings),
        dataset_storage=create_dataset_storage(settings),
        reasoner=create_investigation_reasoner(settings),
        settings=settings,
        step_delay_seconds=settings.step_execution_delay_seconds,
        memory_repository=create_memory_repository(settings),
    )


@lru_cache
def get_memory_service() -> MemoryService:
    settings = get_app_settings()
    return MemoryService(create_memory_repository(settings))


@lru_cache
def get_dataset_service() -> DatasetService:
    """Build dataset service with configured storage and persistence."""
    settings = get_app_settings()
    return DatasetService(
        repository=create_dataset_repository(settings),
        storage=create_dataset_storage(settings),
        settings=settings,
    )


@lru_cache
def get_mission_worker() -> MissionWorker:
    """Worker used by local dispatch and the Pub/Sub push endpoint."""
    settings = get_app_settings()
    return MissionWorker(
        repository=create_mission_repository(settings),
        workflow_runner=_build_workflow_runner(settings),
        settings=settings,
    )


@lru_cache
def get_mission_service() -> MissionService:
    """Build mission service with configured dependencies."""
    settings = get_app_settings()
    repository = create_mission_repository(settings)
    dataset_repository = create_dataset_repository(settings)
    if settings.resolved_dispatcher == DispatcherBackend.PUBSUB:
        dispatcher = create_dispatcher(settings)
    else:
        dispatcher = create_dispatcher(settings, get_mission_worker())
    recovery = MissionRecoveryService(repository=repository, dispatcher=dispatcher)
    return MissionService(
        repository=repository,
        dispatcher=dispatcher,
        recovery=recovery,
        settings=settings,
        dataset_repository=dataset_repository,
    )
