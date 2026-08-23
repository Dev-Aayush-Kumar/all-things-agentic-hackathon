"""ADK-backed tool selection for the agent loop.

When Gemini credentials are configured, the model proposes which allowlisted
tools to run. Invalid tools are dropped. Facts are still measured by Python tools.
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
    """Ask Gemini via ADK which tools to run. Falls back to local policy on failure."""
    fallback = select_tools(goal)
    try:
        from google.adk import Agent
        from google.adk.apps import App
        from google.adk.runners import InMemoryRunner
    except ImportError:
        logger.warning("google-adk is not available; using local tool selection")
        return fallback

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
        logger.exception("ADK tool selection failed; using local policy")
        return fallback


def build_adk_investigator_agent(settings: Settings):
    """Build an ADK Agent wired to FunctionTools. Used when credentials exist.

    Tool functions are wrappers that refuse to run without a bound context; the
    production loop still executes tools locally so evidence and limits stay
    under ATLAS control. This constructor verifies the ADK 2.x tools API.
    """
    from google.adk import Agent
    from google.adk.tools import FunctionTool

    def profile_dataset() -> dict[str, str]:
        return {"status": "bound_at_runtime"}

    def analyze_missing_values() -> dict[str, str]:
        return {"status": "bound_at_runtime"}

    def analyze_duplicates() -> dict[str, str]:
        return {"status": "bound_at_runtime"}

    def analyze_type_format() -> dict[str, str]:
        return {"status": "bound_at_runtime"}

    def analyze_outliers() -> dict[str, str]:
        return {"status": "bound_at_runtime"}

    def analyze_consistency() -> dict[str, str]:
        return {"status": "bound_at_runtime"}

    def inspect_column(column_name: str) -> dict[str, str]:
        return {"status": "bound_at_runtime", "column_name": column_name}

    return Agent(
        name="atlas_investigator",
        model=settings.gemini_model,
        instruction=SELECTOR_INSTRUCTION,
        description="ATLAS investigator with allowlisted data-quality tools.",
        tools=[
            FunctionTool(profile_dataset),
            FunctionTool(analyze_missing_values),
            FunctionTool(analyze_duplicates),
            FunctionTool(analyze_type_format),
            FunctionTool(analyze_outliers),
            FunctionTool(analyze_consistency),
            FunctionTool(inspect_column),
        ],
    )


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
