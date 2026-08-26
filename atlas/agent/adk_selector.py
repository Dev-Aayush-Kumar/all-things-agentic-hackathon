"""ADK-backed tool selection for the local-policy supervisor path.

When Gemini credentials are configured, ``AdkDecisionMaker`` drives the
initial plan and this selector is not used. ``resolve_initial_tools`` still
calls ``select_tools_with_adk`` when a decision-maker does not drive the
initial plan. Facts are always measured by ATLAS Python tools, not by ADK
FunctionTool callbacks.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from atlas.agent.policy import select_tools
from atlas.agent.tools import INVESTIGATION_TOOLS, PROFILE_DATASET
from atlas.config.settings import Settings

logger = logging.getLogger(__name__)

SELECTOR_INSTRUCTION = """You are ATLAS, an autonomous operations investigator.

Select investigation capabilities for a mission. You may ONLY choose from this list:
- profile_dataset
- analyze_missing_values
- analyze_duplicates
- analyze_type_format
- analyze_outliers
- analyze_consistency

Do not invent tools. Do not request shell, code execution, or filesystem access.
Always include profile_dataset. Do not include inspect_column in the initial plan;
that is reserved for later adaptive follow-up after evidence is observed.

Return ONLY JSON:
{
  "objective": "one sentence restatement",
  "selected_tools": ["profile_dataset", "..."]
}
"""


async def select_tools_with_adk(goal: str, settings: Settings) -> list[str]:
    """Ask Gemini via ADK which tools to run. Raises on ADK failure."""
    fallback = select_tools(goal)
    try:
        from google.adk import Agent
        from google.adk.apps import App
        from google.adk.runners import InMemoryRunner
    except ImportError as exc:
        raise RuntimeError("google-adk is not installed") from exc

    try:
        agent = Agent(
            name="atlas_tool_selector",
            model=settings.gemini_model,
            instruction=SELECTOR_INSTRUCTION,
            description="Selects allowlisted ATLAS investigation tools for a mission.",
        )
        app = App(name="atlas_tool_selector_app", root_agent=agent)
        runner = InMemoryRunner(app=app)
        prompt = (
            f"Mission goal:\n{goal}\n\n"
            f"Allowed tools: {list(INVESTIGATION_TOOLS[:-1])}\n"
            "Respond with JSON only."
        )
        events = await runner.run_debug(
            prompt,
            user_id="atlas_system",
            session_id=f"select_{hash(goal) & 0xFFFFFFFF:08x}",
        )
        raw = _final_text(events)
        payload = _extract_json(raw)
        selected = [
            name
            for name in payload.get("selected_tools", [])
            if name in INVESTIGATION_TOOLS and name != "inspect_column"
        ]
        if PROFILE_DATASET not in selected:
            selected = [PROFILE_DATASET, *selected]
        if len(selected) == 1:
            logger.warning("ADK selected only profile; merging local policy tools")
            selected = list(dict.fromkeys([*selected, *fallback]))
        return selected
    except Exception:
        logger.exception("ADK tool selection failed")
        raise


def _final_text(events: list[Any]) -> str:
    for event in reversed(events):
        if hasattr(event, "is_final_response") and event.is_final_response():
            if event.content and event.content.parts:
                return event.content.parts[0].text or ""
        if getattr(event, "content", None) and event.content.parts:
            text = event.content.parts[0].text
            if text:
                return text
    raise RuntimeError("ADK tool selector did not return a response")


def _extract_json(raw_response: str) -> dict[str, Any]:
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
        raise ValueError("Selector JSON must be an object")
    return parsed
