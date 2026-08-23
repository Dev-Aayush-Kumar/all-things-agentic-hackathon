"""ADK integration smoke tests (no cloud credentials required)."""

from atlas.agent.adk_selector import build_adk_investigator_agent
from atlas.config.settings import PlannerBackend, Settings, get_settings


def test_adk_agent_and_function_tools_import() -> None:
    from google.adk import Agent
    from google.adk.apps import App
    from google.adk.runners import InMemoryRunner
    from google.adk.tools import FunctionTool

    def profile_dataset() -> dict[str, str]:
        return {"ok": "true"}

    agent = Agent(
        name="atlas_adk_smoke",
        model="gemini-2.5-flash",
        instruction="Select allowlisted tools only.",
        tools=[FunctionTool(profile_dataset)],
    )
    assert agent.tools
    app = App(name="atlas_adk_smoke_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    assert runner is not None


def test_adk_investigator_builder_uses_allowlisted_tools() -> None:
    settings = Settings(planner_backend="local", gemini_model="gemini-2.5-flash")
    agent = build_adk_investigator_agent(settings)
    assert agent.name == "atlas_investigator"
    assert len(agent.tools) == 7


def test_settings_still_distinguish_local_and_adk() -> None:
    local = Settings(planner_backend="local")
    assert local.resolved_planner_backend == PlannerBackend.LOCAL_FALLBACK
    get_settings.cache_clear()
