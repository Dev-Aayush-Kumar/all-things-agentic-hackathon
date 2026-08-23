"""Gemini + Google ADK investigation reasoner.

Uses measured findings as the only evidence. The model may summarize, explain
impact, and organize a resolution plan. It must not add new findings.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.reasoner_base import InvestigationReasoner, ReasoningResult
from atlas.config.settings import Settings
from atlas.domain.enums import PlannerSource
from atlas.domain.models import DatasetProfile, Finding, RecommendedAction
from atlas.investigation.report import default_actions_from_findings

logger = logging.getLogger(__name__)

REASONER_INSTRUCTION = """You are ATLAS, an autonomous operations investigation reasoner.

You receive a user goal, a measured dataset profile, and a list of findings that
were produced by deterministic Python analysis. You must:

- Interpret the findings in the context of the user's goal
- Summarize likely impact
- Organize a prioritized resolution plan
- NEVER invent findings, metrics, row counts, or columns that are not in the input
- NEVER claim you calculated statistics yourself

Return ONLY valid JSON:
{
  "mission_summary": "...",
  "investigation_summary": "...",
  "overall_assessment": "...",
  "recommended_actions": [
    {
      "title": "...",
      "description": "...",
      "related_finding_ids": ["..."]
    }
  ]
}
"""


class AdkInvestigationReasoner(InvestigationReasoner):
    """Reasoner backed by Google ADK and Gemini."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fallback = LocalFallbackReasoner()

    @property
    def source_name(self) -> str:
        return "GEMINI_ADK"

    async def interpret(
        self,
        goal: str,
        profile: DatasetProfile,
        findings: list[Finding],
    ) -> ReasoningResult:
        fallback = await self._fallback.interpret(goal, profile, findings)
        try:
            raw = await self._invoke_adk(goal, profile, findings)
            parsed = self._parse(raw)
        except Exception:
            logger.exception(
                "ADK investigation reasoner failed; using local fallback interpretation"
            )
            return fallback

        allowed_ids = {finding.finding_id for finding in findings}
        actions = self._actions_from_payload(parsed, allowed_ids, findings)
        return ReasoningResult(
            mission_summary=str(parsed.get("mission_summary") or fallback.mission_summary),
            investigation_summary=str(
                parsed.get("investigation_summary") or fallback.investigation_summary
            ),
            overall_assessment=str(
                parsed.get("overall_assessment") or fallback.overall_assessment
            ),
            recommended_actions=actions or fallback.recommended_actions,
            source=PlannerSource.GEMINI_ADK,
        )

    async def _invoke_adk(
        self,
        goal: str,
        profile: DatasetProfile,
        findings: list[Finding],
    ) -> str:
        from google.adk import Agent
        from google.adk.apps import App
        from google.adk.runners import InMemoryRunner

        agent = Agent(
            name="atlas_investigation_reasoner",
            model=self._settings.gemini_model,
            instruction=REASONER_INSTRUCTION,
            description="Interprets measured data-quality findings against a mission goal.",
        )
        app = App(name="atlas_reasoner_app", root_agent=agent)
        runner = InMemoryRunner(app=app)
        payload = {
            "goal": goal,
            "dataset_profile": profile.model_dump(),
            "findings": [finding.model_dump() for finding in findings],
        }
        prompt = (
            "Interpret these measured investigation results. "
            "Do not invent findings.\n\n"
            f"{json.dumps(payload, default=str)}\n\n"
            "Respond with JSON only."
        )
        events = await runner.run_debug(
            prompt,
            user_id="atlas_system",
            session_id=f"reason_{hash(goal) & 0xFFFFFFFF:08x}",
        )
        for event in reversed(events):
            if hasattr(event, "is_final_response") and event.is_final_response():
                if event.content and event.content.parts:
                    return event.content.parts[0].text or ""
            if getattr(event, "content", None) and event.content.parts:
                text = event.content.parts[0].text
                if text:
                    return text
        raise RuntimeError("ADK reasoner did not return a response")

    def _parse(self, raw_response: str) -> dict[str, Any]:
        text = raw_response.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("Reasoner JSON must be an object")
        return parsed

    def _actions_from_payload(
        self,
        payload: dict[str, Any],
        allowed_ids: set[str],
        findings: list[Finding],
    ) -> list[RecommendedAction]:
        raw_actions = payload.get("recommended_actions") or []
        if not isinstance(raw_actions, list):
            return default_actions_from_findings(findings)
        actions: list[RecommendedAction] = []
        for index, item in enumerate(raw_actions, start=1):
            if not isinstance(item, dict):
                continue
            related = [
                finding_id
                for finding_id in item.get("related_finding_ids") or []
                if finding_id in allowed_ids
            ]
            actions.append(
                RecommendedAction(
                    action_id=f"action_{index}",
                    title=str(item.get("title") or f"Action {index}"),
                    description=str(item.get("description") or ""),
                    related_finding_ids=related,
                    priority=index,
                )
            )
        return actions
