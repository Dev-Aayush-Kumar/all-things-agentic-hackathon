"""Local mission-creation idempotency helpers."""

from __future__ import annotations

import hashlib
import json


def normalize_idempotency_key(value: str | None) -> str | None:
    """Return a usable key, or None when the client omitted one."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def mission_fingerprint(goal: str, dataset_id: str | None) -> str:
    """Stable hash of the mission-creating payload."""
    payload = json.dumps(
        {"goal": goal, "dataset_id": dataset_id},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
