"""Planner factory."""

import logging

from atlas.agent.adk_planner import AdkMissionPlanner
from atlas.agent.adk_reasoner import AdkInvestigationReasoner
from atlas.agent.base import MissionPlanner
from atlas.agent.local_planner import LocalFallbackPlanner
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.reasoner_base import InvestigationReasoner
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


def create_investigation_reasoner(settings: Settings) -> InvestigationReasoner:
    """Create the investigation reasoner matching the configured planner backend."""
    backend = settings.resolved_planner_backend

    if backend == PlannerBackend.ADK:
        if not settings.adk_configured:
            logger.warning(
                "ADK reasoner requested but credentials missing; "
                "falling back to local development reasoner."
            )
            return LocalFallbackReasoner()
        logger.info(
            "Using GEMINI_ADK investigation reasoner with model=%s",
            settings.gemini_model,
        )
        return AdkInvestigationReasoner(settings)

    logger.info("Using LOCAL_DEVELOPMENT_FALLBACK investigation reasoner")
    return LocalFallbackReasoner()
