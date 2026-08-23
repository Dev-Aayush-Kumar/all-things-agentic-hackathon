"""FastAPI application dependencies."""

from functools import lru_cache

from atlas.agent.factory import create_mission_planner
from atlas.config.settings import Settings, get_settings
from atlas.execution.factory import create_background_executor
from atlas.persistence.factory import create_mission_repository
from atlas.services.mission_service import MissionService
from atlas.workflow.mission_runner import MissionWorkflowRunner
from atlas.workflow.step_executor import StepExecutor


@lru_cache
def get_app_settings() -> Settings:
    """Cached settings for dependency injection."""
    return get_settings()


@lru_cache
def get_mission_service() -> MissionService:
    """Build mission service with configured dependencies."""
    settings = get_app_settings()
    repository = create_mission_repository(settings)
    planner = create_mission_planner(settings)
    background_executor = create_background_executor()
    step_executor = StepExecutor(settings)
    workflow_runner = MissionWorkflowRunner(repository, planner, step_executor)
    return MissionService(
        repository=repository,
        planner=planner,
        background_executor=background_executor,
        workflow_runner=workflow_runner,
    )
