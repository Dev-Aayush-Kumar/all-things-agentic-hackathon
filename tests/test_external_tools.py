"""Controlled external tools: FETCH_URL policy, evidence, and supervisor loop."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from atlas.agent.local_decider import LocalDecisionMaker
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.agent.tools import ANALYZE_DUPLICATES, PROFILE_DATASET, ToolContext
from atlas.config.settings import Settings
from atlas.domain.enums import (
    EventType,
    EvidenceSourceType,
    ExternalInvocationStatus,
    ModelDecisionKind,
    PlannerSource,
)
from atlas.domain.exceptions import ExternalToolValidationError, ModelDecisionError
from atlas.domain.models import (
    Mission,
    ModelDecision,
    ProposedExternalRequest,
    WorkingCopyState,
)
from atlas.investigation.parser import parse_csv_bytes
from atlas.ops.decisions import parse_model_decision, validate_decision
from atlas.ops.external.executor import ExternalToolExecutor
from atlas.ops.external.fetch_url import assert_fetch_arguments, fetch_url
from atlas.ops.external.registry import CAPABILITY_FETCH_URL
from atlas.ops.external.ssrf import validate_destination
from atlas.ops.reasoning_context import build_reasoning_context
from atlas.ops.registry import default_registry
from atlas.ops.supervisor import Supervisor
from atlas.ops.workspace import MissionWorkspace
from atlas.storage.local_storage import LocalFileStorage
from tests.conftest import FIXTURES_DIR
from tests.test_model_loop import ScriptedDecisionMaker


def _settings(**kwargs) -> Settings:
    values = {
        "planner_backend": "local",
        "external_tools_enabled": True,
        "fetch_url_enabled": True,
        "fetch_allowed_domains": "127.0.0.1",
        "fetch_allow_loopback": True,
        "fetch_timeout_seconds": 2.0,
        "fetch_max_bytes": 4096,
        "fetch_max_redirects": 2,
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


def _frame(name: str = "clean_numeric.csv"):
    return parse_csv_bytes((FIXTURES_DIR / name).read_bytes())


async def _noop() -> None:
    return None


def _workspace(mission: Mission, settings: Settings | None = None) -> MissionWorkspace:
    return MissionWorkspace(
        mission=mission,
        tool_context=ToolContext("ds", "clean_numeric.csv", _frame()),
        persist=_noop,
        lock=__import__("asyncio").Lock(),
        settings=settings or _settings(),
        reasoner=LocalFallbackReasoner(),
        registry=default_registry(),
        plan_source=PlannerSource.GEMINI_ADK,
    )


def _closed_settings(**kwargs) -> Settings:
    values = {
        "planner_backend": "local",
        "external_tools_enabled": True,
        "fetch_url_enabled": True,
        "fetch_allowed_domains": "",
        "fetch_allow_loopback": False,
        "fetch_timeout_seconds": 2.0,
        "fetch_max_bytes": 4096,
        "fetch_max_redirects": 2,
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


class _Handler(BaseHTTPRequestHandler):
    body = b"<html><title>Approved methodology</title><body>Use unique keys.</body></html>"
    delay = 0.0
    redirect_to = None
    huge = False

    def do_GET(self) -> None:
        if self.delay:
            time.sleep(self.delay)
        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        payload = self.body
        if self.huge:
            payload = b"X" * 20_000
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


def _serve(**kwargs) -> tuple[HTTPServer, str]:
    handler = type("H", (_Handler,), kwargs)
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/"


@pytest.fixture
def local_server():
    server, url = _serve()
    try:
        yield url
    finally:
        server.shutdown()


def test_fetch_url_arguments_reject_headers_and_secrets() -> None:
    with pytest.raises(ExternalToolValidationError, match="rejects model-controlled"):
        assert_fetch_arguments(
            {
                "url": "https://example.com",
                "headers": {"Authorization": "secret"},
                "cookies": "a=b",
            }
        )


def test_localhost_and_loopback_are_rejected() -> None:
    settings = _closed_settings()
    for url in (
        "http://localhost/secret",
        "http://127.0.0.1/secret",
        "http://[::1]/secret",
    ):
        with pytest.raises(ExternalToolValidationError):
            validate_destination(url, settings)


def test_private_and_link_local_are_rejected() -> None:
    settings = _closed_settings()
    for url in (
        "http://10.0.0.8/x",
        "http://192.168.1.10/x",
        "http://172.16.0.4/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[fc00::1]/x",
    ):
        with pytest.raises(ExternalToolValidationError):
            validate_destination(url, settings)


def test_unsupported_schemes_and_credentials_are_rejected() -> None:
    settings = _closed_settings(fetch_allowed_domains="example.com")
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/1",
        "javascript:alert(1)",
        "http://user:pass@example.com/docs",
    ):
        with pytest.raises(ExternalToolValidationError):
            validate_destination(url, settings)


def test_empty_allowlist_is_fail_closed() -> None:
    settings = _closed_settings()
    with pytest.raises(ExternalToolValidationError, match="allowlist"):
        validate_destination("https://example.com/docs", settings)


def test_unknown_and_forbidden_external_capabilities_are_rejected() -> None:
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission, _settings())
    with pytest.raises(ModelDecisionError, match="forbidden"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "EXTERNAL",
                    "reason": "hack",
                    "external": {"capability": "WEB_FETCH", "arguments": {"url": "http://127.0.0.1/"}},
                }
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError, match="Unknown external"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "EXTERNAL",
                    "reason": "hack",
                    "external": {"capability": "made_up_tool", "arguments": {"url": "http://127.0.0.1/"}},
                }
            ),
            workspace,
        )
    with pytest.raises(ModelDecisionError, match="forbidden"):
        validate_decision(
            parse_model_decision(
                {
                    "decision": "DELEGATE",
                    "reason": "shell",
                    "tasks": [{"capability": "EXECUTE_SHELL"}],
                }
            ),
            workspace,
        )


def test_model_cannot_pass_headers_through_decision() -> None:
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission, _settings())
    with pytest.raises(ModelDecisionError, match="rejects"):
        validate_decision(
            ModelDecision(
                decision=ModelDecisionKind.EXTERNAL,
                reason="steal",
                external=ProposedExternalRequest(
                    capability=CAPABILITY_FETCH_URL,
                    arguments={
                        "url": "http://127.0.0.1/",
                        "headers": {"Authorization": "Bearer abc"},
                        "timeout": 0.1,
                    },
                ),
            ),
            workspace,
        )


@pytest.mark.asyncio
async def test_registered_tool_fetches_local_server_and_records_provenance(
    local_server: str,
) -> None:
    settings = _settings()
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission, settings)
    executor = ExternalToolExecutor()
    invocation = await executor.invoke(
        workspace,
        capability=CAPABILITY_FETCH_URL,
        arguments={"url": local_server},
        reason="need methodology",
        source=PlannerSource.GEMINI_ADK,
    )
    assert invocation.status == ExternalInvocationStatus.SUCCEEDED
    assert invocation.evidence_id
    evidence = next(
        item for item in mission.evidence_records if item.evidence_id == invocation.evidence_id
    )
    assert evidence.source_type == EvidenceSourceType.EXTERNAL
    assert evidence.execution_status == "SUCCEEDED"
    assert evidence.source_url
    assert evidence.observed_facts["title"] == "Approved methodology"
    assert "unique keys" in evidence.observed_facts["excerpt"]
    assert evidence.observed_facts["status_code"] == 200
    context = build_reasoning_context(workspace)
    assert context["external_evidence"]
    assert context["external_evidence"][0]["excerpt"]
    assert "html" not in context["external_evidence"][0]["excerpt"].lower() or "title" not in (
        context["external_evidence"][0]["excerpt"]
    )


@pytest.mark.asyncio
async def test_redirect_cannot_bypass_private_network_policy() -> None:
    settings = _settings()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "http://10.0.0.9/secret"})
    )
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False
    ) as client:
        with pytest.raises(ExternalToolValidationError, match="not allowed"):
            await fetch_url("http://127.0.0.1/go", settings, client=client)


@pytest.mark.asyncio
async def test_oversized_response_is_rejected() -> None:
    server, url = _serve(huge=True)
    try:
        settings = _settings(fetch_max_bytes=1024)
        with pytest.raises(Exception, match="size limit"):
            await fetch_url(url, settings)
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_timeout_is_enforced() -> None:
    server, url = _serve(delay=1.5)
    try:
        settings = _settings(fetch_timeout_seconds=0.2)
        with pytest.raises(Exception, match="timed out"):
            await fetch_url(url, settings)
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_failed_fetch_is_not_successful_evidence(local_server: str) -> None:
    settings = _settings()
    transport = httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ConnectError("down")))
    mission = Mission(goal="Investigate", dataset_id="ds")
    workspace = _workspace(mission, settings)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False
    ) as client:
        executor = ExternalToolExecutor(client=client)
        invocation = await executor.invoke(
            workspace,
            capability=CAPABILITY_FETCH_URL,
            arguments={"url": local_server},
            reason="try",
            source=PlannerSource.LOCAL_FALLBACK,
        )
    assert invocation.status == ExternalInvocationStatus.FAILED
    assert invocation.evidence_id is None
    assert not any(
        item.source_type == EvidenceSourceType.EXTERNAL for item in mission.evidence_records
    )
    context = build_reasoning_context(workspace)
    assert context["external_failures"]
    assert not context["external_evidence"]


@pytest.mark.asyncio
async def test_supervisor_continues_after_nonfatal_external_failure(tmp_path: Path) -> None:
    raw = (FIXTURES_DIR / "clean_numeric.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    decider = ScriptedDecisionMaker(
        [
            {
                "decision": "DELEGATE",
                "reason": "profile",
                "tasks": [{"capability": "profile_dataset"}],
            },
            {
                "decision": "EXTERNAL",
                "reason": "compare",
                "external": {"capability": "FETCH_URL", "arguments": {"url": "http://127.0.0.1/missing"}},
            },
            {
                "decision": "OBSERVE",
                "reason": "continue after failure",
                "tool": {"name": "analyze_duplicates", "arguments": {}},
            },
            {
                "decision": "COMPLETE",
                "reason": "enough evidence",
                "summary": "done",
            },
        ]
    )
    mission = Mission(goal="Analyze this numeric CSV.", dataset_id="ds")
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="clean_numeric.csv",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.GEMINI_ADK,
        dataset_storage=storage,
        decision_maker=decider,
        external_executor=ExternalToolExecutor(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _r: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))),
                follow_redirects=False,
                trust_env=False,
            )
        ),
    )
    await supervisor.run(mission, ToolContext("ds", "clean_numeric.csv", parse_csv_bytes(raw)), _noop)
    assert any(item.status == ExternalInvocationStatus.FAILED for item in mission.external_invocations)
    assert any(record.tool_name == ANALYZE_DUPLICATES for record in mission.evidence_records)
    assert mission.investigation_report is not None
    assert EventType.EXTERNAL_TOOL_FAILED in {event.type for event in mission.events}


@pytest.mark.asyncio
async def test_scripted_loop_observe_external_observe_complete(
    tmp_path: Path, local_server: str
) -> None:
    raw = (FIXTURES_DIR / "clean_numeric.csv").read_bytes()
    storage = LocalFileStorage(tmp_path)
    await storage.save("source.csv", raw)
    decider = ScriptedDecisionMaker(
        [
            {
                "decision": "DELEGATE",
                "reason": "measure",
                "tasks": [{"capability": "profile_dataset"}],
            },
            {
                "decision": "EXTERNAL",
                "reason": "approved methodology",
                "external": {"capability": "FETCH_URL", "arguments": {"url": local_server}},
            },
            {
                "decision": "OBSERVE",
                "reason": "duplicates after reference",
                "tool": {"name": "analyze_duplicates", "arguments": {}},
            },
            {
                "decision": "COMPLETE",
                "reason": "goal can be answered",
                "summary": "compared against approved reference",
            },
        ]
    )
    mission = Mission(
        goal=f"Analyze this CSV and compare findings to {local_server}",
        dataset_id="ds",
    )
    mission.working_copy = WorkingCopyState(
        source_dataset_id="ds",
        source_stored_filename="source.csv",
        source_original_filename="clean_numeric.csv",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.GEMINI_ADK,
        dataset_storage=storage,
        decision_maker=decider,
    )
    await supervisor.run(mission, ToolContext("ds", "clean_numeric.csv", parse_csv_bytes(raw)), _noop)
    kinds = [
        record.decision.decision
        for record in mission.reasoning_trace
        if record.decision is not None and record.accepted
    ]
    assert ModelDecisionKind.EXTERNAL in kinds
    assert ModelDecisionKind.OBSERVE in kinds
    assert ModelDecisionKind.COMPLETE in kinds
    assert any(item.status == ExternalInvocationStatus.SUCCEEDED for item in mission.external_invocations)
    external = [item for item in mission.evidence_records if item.source_type == EvidenceSourceType.EXTERNAL]
    assert external
    later = next(ctx for ctx in decider.seen_contexts if ctx.get("external_evidence"))
    assert later["external_evidence"][0]["excerpt"]
    assert mission.investigation_report is not None
    assert mission.investigation_report.external_references
    assert mission.investigation_report.findings is not None
    events = {event.type for event in mission.events}
    assert EventType.EXTERNAL_TOOL_AUTHORIZED in events
    assert EventType.EXTERNAL_TOOL_COMPLETED in events
    catalog_names = {item["name"] for item in later["allowed_capabilities"]}
    assert CAPABILITY_FETCH_URL in catalog_names


@pytest.mark.asyncio
async def test_local_fallback_proposes_allowlisted_goal_url_only(local_server: str) -> None:
    mission = Mission(
        goal=f"Inspect this dataset and compare findings against {local_server}",
        dataset_id="ds",
    )
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(),
        plan_source=PlannerSource.LOCAL_FALLBACK,
        decision_maker=LocalDecisionMaker(),
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "clean_numeric.csv", _frame()),
        _noop,
    )
    assert any(item.tool_name == CAPABILITY_FETCH_URL for item in mission.external_invocations)
    assert all(record.source == PlannerSource.LOCAL_FALLBACK for record in mission.reasoning_trace)
    assert mission.investigation_report is not None
    assert mission.investigation_report.reasoning_source == PlannerSource.LOCAL_FALLBACK


@pytest.mark.asyncio
async def test_dataset_only_local_mission_does_not_fetch() -> None:
    mission = Mission(goal="Analyze quality problems in this dataset.", dataset_id="ds")
    supervisor = Supervisor(
        reasoner=LocalFallbackReasoner(),
        settings=_settings(fetch_allowed_domains="example.com", fetch_allow_loopback=False),
        plan_source=PlannerSource.LOCAL_FALLBACK,
        decision_maker=LocalDecisionMaker(),
    )
    await supervisor.run(
        mission,
        ToolContext("ds", "survey_quality.csv", _frame("survey_quality.csv")),
        _noop,
    )
    assert mission.external_invocations == []
    assert mission.investigation_report is not None
    assert mission.investigation_report.external_references == []


def test_fetch_url_is_in_catalog_when_enabled() -> None:
    from atlas.ops.capabilities import capability_catalog

    names = {item.name for item in capability_catalog(_settings())}
    assert CAPABILITY_FETCH_URL in names
    disabled = capability_catalog(_settings(external_tools_enabled=False))
    assert CAPABILITY_FETCH_URL not in {item.name for item in disabled}
