"""Step execution logic."""

import asyncio
import logging

from atlas.config.settings import Settings
from atlas.domain.enums import StepStatus
from atlas.domain.models import PlanStep

logger = logging.getLogger(__name__)


class StepExecutor:
    """Executes individual plan steps."""

    def __init__(self, settings: Settings) -> None:
        self._delay = settings.step_execution_delay_seconds

    async def execute(self, step: PlanStep, goal: str) -> PlanStep:
        """Execute a single plan step and return updated step."""
        step.status = StepStatus.IN_PROGRESS
        await asyncio.sleep(self._delay)

        try:
            step.result = self._simulate_step_result(step, goal)
            step.status = StepStatus.COMPLETED
        except Exception as exc:
            step.status = StepStatus.FAILED
            step.error = str(exc)
            logger.exception("Step %s failed", step.id)

        return step

    @staticmethod
    def _simulate_step_result(step: PlanStep, goal: str) -> str:
        """Produce a meaningful result for Round 1 step execution."""
        return (
            f"Completed '{step.title}' for mission goal: {goal}. "
            f"{step.description}"
        )
