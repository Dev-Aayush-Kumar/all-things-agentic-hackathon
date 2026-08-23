"""FastAPI application dependencies."""

from functools import lru_cache

from atlas.agent.factory import create_investigation_reasoner, create_mission_planner
from atlas.config.settings import Settings, get_settings
from atlas.execution.factory import create_background_executor
from atlas.persistence.factory import create_dataset_repository, create_mission_repository
from atlas.services.dataset_service import DatasetService
from atlas.services.mission_service import MissionService
from atlas.storage.factory import create_dataset_storage
from atlas.workflow.mission_runner import MissionWorkflowRunner
from atlas.workflow.step_executor import StepExecutor


@lru_cache
def get_app_settings() -> Settings:
    """Cached settings for dependency injection."""
    return get_settings()


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
def get_mission_service() -> MissionService:
    """Build mission service with configured dependencies."""
    settings = get_app_settings()
    repository = create_mission_repository(settings)
    dataset_repository = create_dataset_repository(settings)
    dataset_storage = create_dataset_storage(settings)
    planner = create_mission_planner(settings)
    reasoner = create_investigation_reasoner(settings)
    background_executor = create_background_executor()
    step_executor = StepExecutor(settings)
    workflow_runner = MissionWorkflowRunner(
        repository=repository,
        planner=planner,
        step_executor=step_executor,
        dataset_repository=dataset_repository,
        dataset_storage=dataset_storage,
        reasoner=reasoner,
        settings=settings,
        step_delay_seconds=settings.step_execution_delay_seconds,
    )
    return MissionService(
        repository=repository,
        planner=planner,
        background_executor=background_executor,
        workflow_runner=workflow_runner,
        dataset_repository=dataset_repository,
    )
