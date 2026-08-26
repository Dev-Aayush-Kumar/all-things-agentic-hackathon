"""ADK integration smoke tests (no cloud credentials required).

Production uses google.adk Agent + InMemoryRunner for typed JSON decisions.
ATLAS executes tools; ADK FunctionTool is not on the runtime path.
"""

from atlas.agent.adk_decider import AdkDecisionMaker
from atlas.agent.gemini import DEFAULT_GEMINI_MODEL
from atlas.config.settings import PlannerBackend, Settings, get_settings


def test_adk_supervisor_runner_api_imports() -> None:
    from google.adk import Agent
    from google.adk.apps import App
    from google.adk.runners import InMemoryRunner

    agent = Agent(
        name="atlas_adk_smoke",
        model=DEFAULT_GEMINI_MODEL,
        instruction="Propose one typed ATLAS decision as JSON. Do not execute tools.",
    )
    app = App(name="atlas_adk_smoke_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    assert runner is not None


def test_adk_decision_maker_does_not_bind_function_tools() -> None:
    settings = Settings(
        _env_file=None,
        planner_backend="adk",
        google_api_key="not-a-secret-for-tests",
        gemini_model=DEFAULT_GEMINI_MODEL,
    )
    maker = AdkDecisionMaker(settings)
    assert maker.source.value == "GEMINI_ADK"
    assert maker.drives_initial_plan is True
    assert not hasattr(maker, "tools")


def test_settings_still_distinguish_local_and_adk() -> None:
    local = Settings(_env_file=None, planner_backend="local")
    assert local.resolved_planner_backend == PlannerBackend.LOCAL_FALLBACK
    assert local.planner_label == "LOCAL_DEVELOPMENT_FALLBACK"
    get_settings.cache_clear()
