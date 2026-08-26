"""Gemini decision-path hardening. Does not call the live Gemini API."""

from __future__ import annotations

import asyncio
import os

import pytest

from atlas.agent.adk_decider import AdkDecisionMaker, _extract_json
from atlas.agent.adk_planner import AdkMissionPlanner
from atlas.agent.factory import create_decision_maker, create_mission_planner
from atlas.agent.gemini_schema import (
    gemini_developer_output_schema,
    schema_contains_additional_properties,
)
from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.local_planner import LocalFallbackPlanner
from atlas.config.settings import PlannerBackend, Settings
from atlas.domain.exceptions import ModelDecisionError
from atlas.runtime.safe_errors import (
    categorize_planner_failure,
    describe_planner_failure,
    sanitize_error_message,
)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


async def _noop() -> None:
    """Await-able persistence stub. Supervisor calls `await persist()`."""
    return None


def test_planner_backend_gemini_is_an_adk_alias() -> None:
    with_key = _settings(planner_backend="gemini", google_api_key="not-a-secret-for-tests")
    assert with_key.resolved_planner_backend == PlannerBackend.ADK
    assert with_key.planner_label == "REAL_GEMINI_ADK"
    assert isinstance(create_decision_maker(with_key), AdkDecisionMaker)

    without_key = _settings(planner_backend="gemini", google_api_key=None)
    assert without_key.resolved_planner_backend == PlannerBackend.ADK
    assert without_key.planner_label == "LOCAL_DEVELOPMENT_FALLBACK"
    assert isinstance(create_decision_maker(without_key), LocalDecisionMaker)
    assert isinstance(create_mission_planner(without_key), LocalFallbackPlanner)


def test_export_adk_runtime_env_copies_blank_process_key_without_logging() -> None:
    settings = _settings(google_api_key="not-a-secret-for-tests")
    previous = os.environ.get("GOOGLE_API_KEY")
    try:
        os.environ["GOOGLE_API_KEY"] = ""
        settings.export_adk_runtime_env()
        assert os.environ.get("GOOGLE_API_KEY") == "not-a-secret-for-tests"
        dumped = str(settings.public_diagnostics())
        assert "not-a-secret-for-tests" not in dumped
        assert "google_api_key" not in dumped
        os.environ["GOOGLE_API_KEY"] = "already-in-process"
        other = _settings(google_api_key="should-not-overwrite")
        other.export_adk_runtime_env()
        assert os.environ.get("GOOGLE_API_KEY") == "already-in-process"
    finally:
        if previous is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = previous


def test_create_decision_maker_exports_settings_key_into_process_env() -> None:
    previous = os.environ.get("GOOGLE_API_KEY")
    try:
        os.environ.pop("GOOGLE_API_KEY", None)
        settings = _settings(
            planner_backend="gemini",
            google_api_key="not-a-secret-for-tests",
        )
        maker = create_decision_maker(settings)
        assert isinstance(maker, AdkDecisionMaker)
        assert os.environ.get("GOOGLE_API_KEY") == "not-a-secret-for-tests"
    finally:
        if previous is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = previous


@pytest.mark.asyncio
async def test_dataset_mission_skips_adk_planner_when_supervisor_drives() -> None:
    from atlas.domain.models import Mission
    from atlas.workflow.mission_runner import MissionWorkflowRunner
    from atlas.workflow.step_executor import StepExecutor

    settings = _settings(
        planner_backend="adk",
        google_api_key="not-a-secret-for-tests",
    )
    planner = AdkMissionPlanner(settings)

    async def _should_not_run(goal: str, dataset_id: str | None = None):
        raise AssertionError("AdkMissionPlanner.create_plan must not run for dataset missions")

    planner.create_plan = _should_not_run  # type: ignore[method-assign]
    mission = Mission(goal="Profile this dataset and find duplicates.", dataset_id="ds-1")
    runner = MissionWorkflowRunner(
        repository=None,  # type: ignore[arg-type]
        planner=planner,
        step_executor=StepExecutor(settings),
        settings=settings,
    )
    persist_calls: list[str] = []

    async def _persist(_mission=None) -> None:
        persist_calls.append(mission.status.value)

    runner._persist = _persist  # type: ignore[method-assign]
    await runner._plan(mission)
    assert mission.execution_plan is not None
    assert mission.execution_plan.planner_source.value == "GEMINI_ADK"
    assert mission.execution_plan.steps[0].id == "step_1"
    assert "Supervisor-driven" in mission.execution_plan.steps[0].title
    planning = next(event for event in mission.events if event.type.value == "PLANNING_STARTED")
    assert planning.metadata["deferred_to_supervisor"] is True


@pytest.mark.asyncio
async def test_generic_mission_still_invokes_planner() -> None:
    from atlas.domain.models import ExecutionPlan, Mission, PlanStep
    from atlas.domain.enums import PlannerSource, StepStatus
    from atlas.workflow.mission_runner import MissionWorkflowRunner
    from atlas.workflow.step_executor import StepExecutor

    settings = _settings(
        planner_backend="adk",
        google_api_key="not-a-secret-for-tests",
    )
    planner = AdkMissionPlanner(settings)
    calls: list[tuple[str, str | None]] = []

    async def _record(goal: str, dataset_id: str | None = None):
        calls.append((goal, dataset_id))
        return ExecutionPlan(
            steps=[
                PlanStep(
                    id="step_1",
                    title="Understand goal",
                    description="Generic step",
                    status=StepStatus.PENDING,
                )
            ],
            planner_source=PlannerSource.GEMINI_ADK,
            summary="generic",
        )

    planner.create_plan = _record  # type: ignore[method-assign]
    mission = Mission(goal="Summarize open incidents.")
    runner = MissionWorkflowRunner(
        repository=None,  # type: ignore[arg-type]
        planner=planner,
        step_executor=StepExecutor(settings),
        settings=settings,
    )

    async def _persist(_mission=None) -> None:
        return None

    runner._persist = _persist  # type: ignore[method-assign]
    await runner._plan(mission)
    assert calls == [("Summarize open incidents.", None)]
    assert mission.execution_plan is not None
    assert mission.execution_plan.steps[0].title == "Understand goal"


def test_local_backend_never_selects_gemini_even_with_credentials() -> None:
    settings = _settings(planner_backend="local", google_api_key="not-a-secret-for-tests")
    assert settings.resolved_planner_backend == PlannerBackend.LOCAL_FALLBACK
    assert settings.planner_label == "LOCAL_DEVELOPMENT_FALLBACK"
    maker = create_decision_maker(settings)
    assert isinstance(maker, LocalDecisionMaker)
    assert maker.source.value == "LOCAL_FALLBACK"


def test_adk_decision_maker_cannot_execute_or_persist() -> None:
    assert not hasattr(AdkDecisionMaker, "execute")
    assert not hasattr(AdkDecisionMaker, "approve")
    assert not hasattr(AdkDecisionMaker, "run_tool")
    assert not hasattr(AdkDecisionMaker, "upsert_experience")
    assert not hasattr(AdkDecisionMaker, "create_approval")


def test_pydantic_model_decision_schema_includes_additional_properties() -> None:
    from atlas.domain.models import ModelDecision

    raw = ModelDecision.model_json_schema()
    assert schema_contains_additional_properties(raw)


def test_gemini_developer_output_schema_omits_additional_properties() -> None:
    from atlas.domain.models import ModelDecision

    schema = gemini_developer_output_schema(ModelDecision)
    assert schema.get("type") == "object"
    assert "decision" in schema["properties"]
    assert "reason" in schema["properties"]
    assert not schema_contains_additional_properties(schema)
    assert "additionalProperties" not in str(schema)


def test_extract_json_rejects_malformed_gemini_text() -> None:
    with pytest.raises(ModelDecisionError, match="not valid JSON"):
        _extract_json("sorry, I cannot produce JSON today")
    with pytest.raises(ModelDecisionError, match="JSON must be an object"):
        _extract_json("[1, 2, 3]")


def test_extra_gemini_fields_cannot_approve_or_execute() -> None:
    from atlas.ops.decisions import parse_model_decision

    parsed = parse_model_decision(
        {
            "decision": "OBSERVE",
            "reason": "look",
            "tool": {"name": "profile_dataset", "arguments": {}},
            "execute_shell": "rm -rf /",
            "approve": True,
        }
    )
    dumped = parsed.model_dump()
    assert "execute_shell" not in dumped
    assert "approve" not in dumped
    assert parsed.decision.value == "OBSERVE"


def test_sanitize_error_message_redacts_credentials() -> None:
    leaked = (
        "POST https://generativelanguage.googleapis.com/v1beta/models?"
        "key=AIzaSyFakeTestKeyValueNotReal00 failed with "
        "Authorization: Bearer super-secret-token"
    )
    cleaned = sanitize_error_message(leaked)
    assert "AIzaSyFakeTestKeyValueNotReal00" not in cleaned
    assert "super-secret-token" not in cleaned
    assert "Authorization" not in cleaned or "[REDACTED]" in cleaned
    assert "[REDACTED]" in cleaned
    assert "key=" not in cleaned or "key=[REDACTED]" in cleaned
    assert "?" not in cleaned


def test_categorize_planner_failure_is_coarse_and_safe() -> None:
    assert categorize_planner_failure(TimeoutError("waited too long")) == "timeout"
    assert categorize_planner_failure(ModelDecisionError("Model response was not valid JSON")) == "malformed_json"
    assert categorize_planner_failure(ConnectionError("network unreachable")) == "network"
    assert categorize_planner_failure(RuntimeError("adk timeout")) == "timeout"


def _client_error(status: int, message: str, provider_status: str = "INVALID_ARGUMENT"):
    from google.genai.errors import ClientError

    return ClientError(
        status,
        {
            "error": {
                "code": status,
                "message": message,
                "status": provider_status,
            }
        },
    )


def test_describe_planner_failure_never_leaks_secrets() -> None:
    exc = _client_error(
        400,
        "Invalid schema using key=AIzaSyFakeTestKeyValueNotReal00 "
        "Authorization: Bearer super-secret-token "
        "https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyFakeTestKeyValueNotReal00",
        "INVALID_ARGUMENT",
    )
    dumped = str(describe_planner_failure(exc))
    assert "AIzaSyFakeTestKeyValueNotReal00" not in dumped
    assert "super-secret-token" not in dumped
    assert "Bearer super-secret-token" not in dumped
    assert "GOOGLE_API_KEY" not in dumped
    assert "?key=" not in dumped


def test_describe_planner_failure_extracts_http_and_provider_fields() -> None:
    inner = _client_error(
        400,
        "Invalid JSON payload received. Unknown name additionalProperties in response_schema",
        "INVALID_ARGUMENT",
    )
    try:
        raise inner
    except Exception as caught:
        wrapped = RuntimeError("ADK runner failed")
        wrapped.__cause__ = caught
    described = describe_planner_failure(wrapped)
    assert described["failure_category"] == "api_error"
    assert described["failure_stage"] == "request_schema"
    assert described["exception_class"] == "RuntimeError"
    assert described["cause_class"] == "ClientError"
    assert described["http_status"] == 400
    assert described["provider_status"] == "INVALID_ARGUMENT"
    assert described["exception_module"].startswith("builtins")


def test_describe_planner_failure_classifies_auth_quota_and_not_found() -> None:
    auth = describe_planner_failure(_client_error(403, "API key not valid", "PERMISSION_DENIED"))
    assert auth["failure_category"] == "api_error"
    assert auth["failure_stage"] == "authentication"
    assert auth["http_status"] == 403

    quota = describe_planner_failure(_client_error(429, "quota exceeded", "RESOURCE_EXHAUSTED"))
    assert quota["failure_category"] == "api_error"
    assert quota["failure_stage"] == "quota"

    missing = describe_planner_failure(_client_error(404, "model not found", "NOT_FOUND"))
    assert missing["failure_category"] == "api_error"
    assert missing["failure_stage"] == "model_lookup"


def test_fallback_behavior_is_unchanged_for_timeouts() -> None:
    assert categorize_planner_failure(TimeoutError("waited too long")) == "timeout"
    described = describe_planner_failure(TimeoutError("waited too long"))
    assert described["failure_category"] == "timeout"
    assert described["failure_stage"] == "timeout"


@pytest.mark.asyncio
async def test_adk_decision_timeout_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        planner_backend="adk",
        google_api_key="not-a-secret-for-tests",
        gemini_timeout_seconds=0.01,
    )
    maker = AdkDecisionMaker(settings)

    async def _slow(_context: dict) -> str:
        await asyncio.sleep(1)
        return '{"decision":"COMPLETE","reason":"too late"}'

    monkeypatch.setattr(maker, "_invoke_adk", _slow)
    with pytest.raises(ModelDecisionError, match="timed out"):
        await maker.decide({"mission_id": "m1", "goal": "profile dataset"})


@pytest.mark.asyncio
async def test_gemini_api_error_diagnostics_do_not_leak_or_bypass_fallback(
    tmp_path,
) -> None:
    from pathlib import Path

    from atlas.agent.local_reasoner import LocalFallbackReasoner
    from atlas.agent.tools import ToolContext
    from atlas.domain.enums import EventType, PlannerSource
    from atlas.domain.models import Mission, WorkingCopyState
    from atlas.investigation.parser import parse_csv_bytes
    from atlas.ops.supervisor import Supervisor
    from atlas.storage.local_storage import LocalFileStorage
    from tests.conftest import FIXTURES_DIR
    from tests.test_model_loop import ScriptedDecisionMaker

    raw = (FIXTURES_DIR / "clean_numeric.csv").read_bytes()
    storage = LocalFileStorage(Path(tmp_path))
    await storage.save("source.csv", raw)
    decider = ScriptedDecisionMaker(
        [
            _client_error(
                400,
                "Invalid JSON payload received. Unknown name additionalProperties "
                "in response_schema Authorization: Bearer super-secret-token "
                "key=AIzaSyFakeTestKeyValueNotReal00",
            )
        ]
    )
    mission = Mission(goal="Analyze quality problems in this dataset.", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="clean_numeric.csv",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(planner_backend="local"),
        plan_source=PlannerSource.GEMINI_ADK,
        dataset_storage=storage,
        decision_maker=decider,
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "clean_numeric.csv", parse_csv_bytes(raw)),
        persist=_noop,
    )
    rejected = next(
        event for event in mission.events if event.type == EventType.MODEL_DECISION_REJECTED
    )
    meta = str(rejected.metadata)
    assert "AIzaSyFakeTestKeyValueNotReal00" not in meta
    assert "super-secret-token" not in meta
    assert rejected.metadata["failure_category"] == "api_error"
    assert rejected.metadata["failure_stage"] == "request_schema"
    assert rejected.metadata["http_status"] == 400
    assert rejected.metadata["exception_class"] == "ClientError"
    assert PlannerSource.LOCAL_FALLBACK in {record.source for record in mission.reasoning_trace}
    assert any(record.accepted and record.source == PlannerSource.LOCAL_FALLBACK for record in mission.reasoning_trace)
