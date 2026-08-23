"""Pub/Sub execution messages. Never include datasets or secrets."""

from __future__ import annotations

import base64
import json
from typing import Any

MESSAGE_SOURCE = "atlas"


def encode_execution_message(mission_id: str) -> bytes:
    """Small JSON payload identifying a durable mission."""
    return json.dumps(
        {"mission_id": mission_id, "source": MESSAGE_SOURCE},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def decode_execution_message(data: bytes) -> str:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Pub/Sub payload must be a JSON object")
    mission_id = payload.get("mission_id")
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise ValueError("Pub/Sub payload missing mission_id")
    return mission_id.strip()


def parse_push_envelope(body: dict[str, Any]) -> str:
    """Extract mission_id from a Pub/Sub push HTTP body or a direct test payload."""
    if "mission_id" in body and isinstance(body.get("mission_id"), str):
        return body["mission_id"].strip()
    message = body.get("message") or {}
    if not isinstance(message, dict):
        raise ValueError("Pub/Sub push body is missing message")
    attributes = message.get("attributes") or {}
    attr_id = attributes.get("mission_id") if isinstance(attributes, dict) else None
    data_b64 = message.get("data")
    if data_b64:
        raw = base64.b64decode(data_b64)
        return decode_execution_message(raw)
    if isinstance(attr_id, str) and attr_id.strip():
        return attr_id.strip()
    raise ValueError("Pub/Sub message does not identify a mission")
