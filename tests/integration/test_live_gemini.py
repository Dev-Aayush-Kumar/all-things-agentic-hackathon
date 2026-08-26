"""Opt-in live Gemini mission. Never runs in the default pytest suite.

Requires ATLAS_RUN_LIVE_GEMINI_TESTS=1 and GOOGLE_API_KEY (or Vertex credentials).
Does not mock Gemini, TLS, validation, governance, or mission execution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.conftest import FIXTURES_DIR, wait_for_mission_status

_EVIDENCE_PATH = Path("artifacts") / "live_gemini_evidence.json"

LIVE_CSV = FIXTURES_DIR / "clean_numeric.csv"
LIVE_GOAL = (
    "Profile this local CSV dataset and identify duplicate rows. "
    "Use only observation or delegation capabilities such as profile_dataset "
    "and analyze_duplicates. Do not propose ACTION remediations, FETCH_URL, "
    "shell commands, or file writes. COMPLETE after you have measured the dataset."
)


def _live_gemini_enabled() -> bool:
    if os.environ.get("ATLAS_RUN_LIVE_GEMINI_TESTS") != "1":
        return False
    if os.environ.get("GOOGLE_API_KEY"):
        return True
    vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
    if vertex and os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return True
    try:
        from atlas.config.settings import Settings

        return bool(Settings().adk_configured)
    except Exception:
        return False


pytestmark = [
    pytest.mark.live_gemini,
    pytest.mark.skipif(
        not _live_gemini_enabled(),
        reason=(
            "Live Gemini tests are opt-in via ATLAS_RUN_LIVE_GEMINI_TESTS=1 "
            "and GOOGLE_API_KEY (or Vertex credentials)"
        ),
    ),
]


@pytest.mark.asyncio
async def test_live_gemini_mission_uses_real_decision_path(client: AsyncClient) -> None:
    from atlas.runtime.tls import native_tls_configured

    assert native_tls_configured() is True

    health = await client.get("/health")
    assert health.status_code == 200
    health_payload = health.json()
    assert "GOOGLE_API_KEY" not in health.text
    assert "google_api_key" not in health_payload
    assert health_payload["planner_label"] == "REAL_GEMINI_ADK"
    assert health_payload["planner_backend"] == "adk"
    assert health_payload["adk_configured"] is True
    assert health_payload["native_tls"] is True
    assert health_payload["gemini_transport"] in {"gemini_api", "vertex_ai"}

    uploaded = await client.post(
        "/datasets",
        files={"file": ("clean_numeric.csv", LIVE_CSV.read_bytes(), "text/csv")},
    )
    assert uploaded.status_code == 201
    dataset_id = uploaded.json()["dataset_id"]

    created = await client.post(
        "/missions",
        json={"goal": LIVE_GOAL, "dataset_id": dataset_id},
    )
    assert created.status_code == 202
    mission_id = created.json()["mission_id"]

    try:
        final = await wait_for_mission_status(
            client,
            mission_id,
            {"COMPLETED", "FAILED", "WAITING_FOR_APPROVAL"},
            timeout=240.0,
        )
    except Exception:
        detail = await client.get(f"/missions/{mission_id}")
        if detail.status_code == 200:
            await _write_safe_evidence(client, health_payload, detail.json(), None)
        raise
    body = final
    assert "GOOGLE_API_KEY" not in str(body)
    if body["status"] == "FAILED":
        await _write_safe_evidence(client, health_payload, body, None)
        pytest.fail(body.get("error") or "live Gemini mission failed")

    event_types = [event["type"] for event in body["events"]]
    sources = [record["source"] for record in body.get("reasoning_trace") or []]
    assert "GEMINI_ADK" in sources, (
        "No GEMINI_ADK reasoning_trace record; Gemini was not actually used"
    )
    assert "MODEL_DECISION_RECEIVED" in event_types
    assert "MODEL_DECISION_VALIDATED" in event_types
    assert "GOVERNANCE_EVALUATED" in event_types

    gemini_events = [
        event
        for event in body["events"]
        if event["type"]
        in {
            "MODEL_REASONING_STARTED",
            "MODEL_DECISION_RECEIVED",
            "MODEL_DECISION_VALIDATED",
            "MODEL_DECISION_REJECTED",
        }
        and (event.get("metadata") or {}).get("source") == "GEMINI_ADK"
    ]
    assert gemini_events, "Mission events did not record GEMINI_ADK provenance"

    if body["status"] == "WAITING_FOR_APPROVAL":
        approvals = await client.get(f"/missions/{mission_id}/approvals")
        assert approvals.status_code == 200
        assert approvals.json()["count"] >= 1
        await _write_safe_evidence(client, health_payload, body, None)
        return

    assert body["status"] == "COMPLETED"
    experience = await client.get(f"/experiences/{mission_id}")
    assert experience.status_code == 200
    exp = experience.json()
    assert exp["mission_id"] == mission_id
    assert "EXPERIENCE_RECORDED" in event_types or "EXPERIENCE_MERGED" in event_types
    assert "STRATEGY_UPDATED" in event_types or "STRATEGY_REJECTED" in event_types
    await _write_safe_evidence(client, health_payload, body, experience.json())


def _safe_events(events: list[dict]) -> list[dict]:
    interesting = {
        "MODEL_REASONING_STARTED",
        "MODEL_DECISION_RECEIVED",
        "MODEL_DECISION_VALIDATED",
        "MODEL_DECISION_REJECTED",
        "MODEL_DECISION_FALLBACK",
        "GOVERNANCE_EVALUATED",
        "GOVERNANCE_DENIED",
        "APPROVAL_REQUESTED",
        "EXPERIENCE_RECORDED",
        "EXPERIENCE_MERGED",
        "STRATEGY_UPDATED",
        "STRATEGY_REJECTED",
        "MISSION_COMPLETED",
        "MISSION_FAILED",
    }
    out: list[dict] = []
    for event in events:
        if event.get("type") not in interesting:
            continue
        metadata = event.get("metadata") or {}
        out.append(
            {
                "type": event.get("type"),
                "source": metadata.get("source"),
                "decision": metadata.get("decision"),
                "fallback": metadata.get("fallback"),
                "failure_category": metadata.get("failure_category"),
                "failure_stage": metadata.get("failure_stage"),
                "exception_class": metadata.get("exception_class"),
                "cause_class": metadata.get("cause_class"),
                "http_status": metadata.get("http_status"),
                "provider_status": metadata.get("provider_status"),
                "provider_code": metadata.get("provider_code"),
                "error": metadata.get("error"),
                "planner_label": metadata.get("planner_label"),
                "verdict": metadata.get("verdict"),
            }
        )
    return out


async def _write_safe_evidence(
    client: AsyncClient,
    health: dict,
    body: dict,
    experience: dict | None = None,
) -> None:
    """Persist inspectable live-run facts. Never writes credentials or prompts."""
    report = body.get("investigation_report") or {}
    approvals_payload = {"count": 0, "items": []}
    if body.get("status") == "WAITING_FOR_APPROVAL":
        approvals = await client.get(f"/missions/{body['mission_id']}/approvals")
        if approvals.status_code == 200:
            approvals_payload = {
                "count": approvals.json().get("count", 0),
                "items": [
                    {
                        "approval_id": item.get("approval_id"),
                        "status": item.get("status"),
                        "capability": item.get("capability"),
                        "operation_kind": item.get("operation_kind"),
                    }
                    for item in approvals.json().get("items") or []
                ],
            }
    strategies_payload: list[dict] = []
    listed = await client.get("/strategies?limit=20")
    if listed.status_code == 200:
        for item in listed.json().get("items") or listed.json().get("strategies") or []:
            strategies_payload.append(
                {
                    "strategy_id": item.get("strategy_id"),
                    "mission_category": item.get("mission_category"),
                    "historical_runs": item.get("historical_runs"),
                    "confidence": item.get("confidence"),
                }
            )
    evidence = {
        "planner_label": health.get("planner_label"),
        "planner_backend": health.get("planner_backend"),
        "gemini_model": health.get("gemini_model"),
        "gemini_transport": health.get("gemini_transport"),
        "native_tls": health.get("native_tls"),
        "adk_configured": health.get("adk_configured"),
        "mission_id": body.get("mission_id"),
        "mission_status": body.get("status"),
        "execution_plan_source": (body.get("execution_plan") or {}).get("planner_source"),
        "report_reasoning_source": report.get("reasoning_source"),
        "finding_count": len(report.get("findings") or []),
        "finding_categories": sorted(
            {item.get("category") for item in report.get("findings") or [] if item.get("category")}
        ),
        "mission_summary": report.get("mission_summary"),
        "overall_assessment": report.get("overall_assessment"),
        "reasoning_trace": [
            {
                "iteration": item.get("iteration"),
                "source": item.get("source"),
                "kind": item.get("kind"),
                "accepted": item.get("accepted"),
                "reason": item.get("reason"),
                "rejection_reason": item.get("rejection_reason"),
            }
            for item in body.get("reasoning_trace") or []
        ],
        "events": _safe_events(body.get("events") or []),
        "governance_events": [
            {
                "verdict": item.get("verdict"),
                "risk": item.get("risk"),
                "reason": item.get("reason"),
            }
            for item in body.get("governance_events") or []
        ],
        "actions": [
            {
                "action_type": item.get("action_type"),
                "status": item.get("status"),
                "verification_passed": item.get("verification_passed"),
            }
            for item in body.get("actions") or []
        ],
        "pending_approval": (
            {
                "approval_id": (body.get("pending_approval") or {}).get("approval_id"),
                "status": (body.get("pending_approval") or {}).get("status"),
                "capability": (body.get("pending_approval") or {}).get("capability"),
            }
            if body.get("pending_approval")
            else None
        ),
        "approvals": approvals_payload,
        "experience": {
            "experience_id": (experience or {}).get("experience_id"),
            "mission_id": (experience or {}).get("mission_id"),
            "outcome": (experience or {}).get("outcome"),
            "model_calls": (experience or {}).get("model_calls"),
            "tool_calls": (experience or {}).get("tool_calls"),
            "success_score": (experience or {}).get("success_score"),
        }
        if experience
        else None,
        "strategies": strategies_payload,
        "strategy_ids_considered": body.get("strategy_ids_considered") or [],
        "event_types": [event.get("type") for event in body.get("events") or []],
    }
    _EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
