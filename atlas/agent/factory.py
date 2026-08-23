"""Planner factory."""

import logging

from atlas.agent.adk_decider import AdkDecisionMaker
from atlas.agent.adk_planner import AdkMissionPlanner
from atlas.agent.adk_reasoner import AdkInvestigationReasoner
from atlas.agent.adk_selector import select_tools_with_adk
from atlas.agent.base import MissionPlanner
from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.local_planner import LocalFallbackPlanner
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.policy import select_tools
from atlas.agent.reasoner_base import InvestigationReasoner
from atlas.config.settings import PlannerBackend, Settings
from atlas.domain.enums import PlannerSource

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
            "Using %s planner with model=%s transport=%s",
            "REAL_GEMINI_ADK",
            settings.gemini_model,
            settings.gemini_transport.value,
        )
        if not settings.gemini_meets_minimum:
            logger.warning(
                "Configured GEMINI_MODEL=%s is below the hackathon minimum Gemini 3.5. "
                "ATLAS will still use this exact model name (no silent downgrade).",
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
            "Using REAL_GEMINI_ADK investigation reasoner with model=%s",
            settings.gemini_model,
        )
        return AdkInvestigationReasoner(settings)

    logger.info("Using LOCAL_DEVELOPMENT_FALLBACK investigation reasoner")
    return LocalFallbackReasoner()


async def resolve_initial_tools(goal: str, settings: Settings) -> tuple[list[str], PlannerSource]:
    """Select initial tools using ADK when configured, otherwise local policy."""
    backend = settings.resolved_planner_backend
    if backend == PlannerBackend.ADK and settings.adk_configured:
        try:
            selected = await select_tools_with_adk(goal, settings)
            logger.info("REAL_GEMINI_ADK selected investigation tools: %s", selected)
            return selected, PlannerSource.GEMINI_ADK
        except Exception:
            logger.exception(
                "ADK tool selection failed; using LOCAL_DEVELOPMENT_FALLBACK tools"
            )
            selected = select_tools(goal)
            return selected, PlannerSource.LOCAL_FALLBACK
    selected = select_tools(goal)
    logger.info("Local policy selected investigation tools: %s", selected)
    return selected, PlannerSource.LOCAL_FALLBACK


def create_decision_maker(settings: Settings):
    """Create the supervisor decision-maker. Local fallback is never labeled Gemini."""
    backend = settings.resolved_planner_backend
    if backend == PlannerBackend.ADK and settings.adk_configured:
        logger.info(
            "Using REAL_GEMINI_ADK supervisor decision-maker with model=%s",
            settings.gemini_model,
        )
        return AdkDecisionMaker(settings)
    logger.info("Using LOCAL_FALLBACK supervisor decision-maker")
    return LocalDecisionMaker()
