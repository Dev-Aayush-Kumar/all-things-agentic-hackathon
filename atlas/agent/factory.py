"""Planner factory."""

import logging

from atlas.agent.adk_planner import AdkMissionPlanner
from atlas.agent.base import MissionPlanner
from atlas.agent.local_planner import LocalFallbackPlanner
from atlas.config.settings import PlannerBackend, Settings

logger = logging.getLogger(__name__)


def create_mission_planner(settings: Settings) -> MissionPlanner:
    """Create the appropriate mission planner based on configuration."""
    backend = settings.resolved_planner_backend

    if backend == PlannerBackend.ADK:
        if not settings.adk_configured:
            logger.warning(
                "ADK planner requested but credentials missing; "
                "falling back to local development planner."
            )
            return LocalFallbackPlanner()

        logger.info(
            "Using GEMINI_ADK planner with model=%s",
            settings.gemini_model,
        )
        return AdkMissionPlanner(settings)

    logger.info("Using LOCAL_DEVELOPMENT_FALLBACK planner")
    return LocalFallbackPlanner()
