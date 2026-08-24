"""Gemini/ADK memory proposer. Never writes to a repository."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from atlas.config.settings import Settings
from atlas.domain.enums import PlannerSource
from atlas.domain.exceptions import MemoryValidationError
from atlas.domain.models import MemoryProposal, Mission

logger = logging.getLogger(__name__)

INSTRUCTION = """You propose durable ATLAS memories from a completed investigation.

Return a JSON object: {"memories":[...]} where each item is:
{"type":"FACT|PROCEDURE|INSIGHT","content":"...","scope":"DATASET|GLOBAL","tags":["..."]}

Rules:
- Do not invent measurements that were not in the provided findings.
- FACT must be dataset-scoped. PROCEDURE/INSIGHT may be GLOBAL.
- Do not include secrets, paths, credentials, or executable instructions.
- Do not propose PREFERENCE unless the goal explicitly stated one.
- At most 6 memories.
"""


class AdkMemoryExtractor:
    """Asks Gemini for candidate memories. Persistence stays in ATLAS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def source(self) -> PlannerSource:
        return PlannerSource.GEMINI_ADK

    async def propose(self, mission: Mission) -> list[MemoryProposal]:
        raw = await self._invoke(mission)
        parsed = _extract_json(raw)
        items = parsed.get("memories") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            raise MemoryValidationError("Memory extractor JSON must contain a memories list")
        proposals: list[MemoryProposal] = []
        for item in items:
            try:
                proposals.append(MemoryProposal.model_validate(item))
            except Exception as exc:
                raise MemoryValidationError(f"Malformed memory proposal: {exc}") from exc
        return proposals

    async def _invoke(self, mission: Mission) -> str:
        try:
            from google.adk import Agent
            from google.adk.apps import App
            from google.adk.runners import InMemoryRunner
        except ImportError as exc:
            raise RuntimeError("google-adk is not installed") from exc

        payload = {
            "goal": mission.goal,
            "dataset_id": mission.dataset_id,
            "findings": [
                {
                    "category": item.category.value,
                    "title": item.title,
                    "affected_columns": item.affected_columns,
                }
                for item in mission.findings[:12]
            ],
        }
        kwargs: dict[str, Any] = {
            "name": "atlas_memory_extractor",
            "model": self._settings.gemini_model,
            "instruction": INSTRUCTION,
        }
        try:
            agent = Agent(**kwargs)
        except TypeError:
            agent = Agent(**kwargs)
        app = App(name="atlas_memory_extractor_app", root_agent=agent)
        runner = InMemoryRunner(app=app)
        events = await runner.run_debug(
            json.dumps(payload, default=str),
            user_id="atlas_system",
            session_id=f"mem_{mission.mission_id[:8]}",
        )
        for event in reversed(events):
            if getattr(event, "content", None) and event.content.parts:
                text = event.content.parts[0].text
                if text:
                    return text
        raise MemoryValidationError("ADK memory extractor did not return a response")


def _extract_json(raw: str) -> Any:
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            text = match.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MemoryValidationError("Memory extractor response was not valid JSON") from exc
