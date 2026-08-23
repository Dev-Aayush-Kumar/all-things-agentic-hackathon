"""Gemini + Google ADK mission planner.

Uses the real Google Agent Development Kit when credentials are configured.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from atlas.agent.base import MissionPlanner
from atlas.config.settings import Settings
from atlas.domain.enums import PlannerSource, StepStatus
from atlas.domain.models import ExecutionPlan, PlanStep

logger = logging.getLogger(__name__)

PLANNER_INSTRUCTION = """You are ATLAS, an autonomous operations planning agent.

Given a high-level mission goal, produce a structured execution plan as JSON.

Return ONLY valid JSON with this exact schema:
{
  "summary": "Brief plan summary",
  "steps": [
    {
      "id": "step_1",
      "title": "Short step title",
      "description": "What this step accomplishes"
    }
  ]
}

Rules:
- Create 3 to 6 actionable steps.
- Step IDs must be unique strings like step_1, step_2, etc.
- Steps must be ordered logically.
- Do not include markdown fences or commentary outside the JSON.
"""


class AdkMissionPlanner(MissionPlanner):
    """Planner backed by Google ADK and Gemini."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def source_name(self) -> str:
        return "GEMINI_ADK"

    async def create_plan(self, goal: str) -> ExecutionPlan:
        """Generate a plan using Google ADK."""
        raw_response = await self._invoke_adk(goal)
        return self._parse_plan(raw_response, goal)

    async def _invoke_adk(self, goal: str) -> str:
        """Call Google ADK agent and return raw text response."""
        try:
            from google.adk import Agent
            from google.adk.apps import App
            from google.adk.runners import InMemoryRunner
        except ImportError as exc:
            raise RuntimeError(
                "google-adk is not installed. Install with: pip install google-adk"
            ) from exc

        agent = Agent(
            name="atlas_planner",
            model=self._settings.gemini_model,
            instruction=PLANNER_INSTRUCTION,
            description="Generates structured execution plans for ATLAS missions.",
        )
        app = App(name="atlas_planner_app", root_agent=agent)
        runner = InMemoryRunner(app=app)

        prompt = (
            f"Create an execution plan for this mission goal:\n\n{goal}\n\n"
            "Respond with JSON only."
        )

        events = await runner.run_debug(
            prompt,
            user_id="atlas_system",
            session_id=f"plan_{hash(goal) & 0xFFFFFFFF:08x}",
        )

        for event in reversed(events):
            if hasattr(event, "is_final_response") and event.is_final_response():
                if event.content and event.content.parts:
                    return event.content.parts[0].text or ""
            if getattr(event, "content", None) and event.content.parts:
                text = event.content.parts[0].text
                if text:
                    return text

        raise RuntimeError("ADK planner did not return a response")

    def _parse_plan(self, raw_response: str, goal: str) -> ExecutionPlan:
        """Parse ADK JSON response into ExecutionPlan."""
        payload = self._extract_json(raw_response)
        steps = [
            PlanStep(
                id=step["id"],
                title=step["title"],
                description=step["description"],
                status=StepStatus.PENDING,
            )
            for step in payload.get("steps", [])
        ]
        if not steps:
            raise ValueError("ADK planner returned an empty plan")

        return ExecutionPlan(
            steps=steps,
            planner_source=PlannerSource.GEMINI_ADK,
            summary=payload.get("summary") or f"ADK plan for: {goal}",
        )

    @staticmethod
    def _extract_json(raw_response: str) -> dict[str, Any]:
        """Extract JSON object from model response."""
        text = raw_response.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)
        return json.loads(text)
