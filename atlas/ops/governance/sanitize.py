"""Sanitize approval payloads. Never persist secrets."""

from __future__ import annotations

from typing import Any

_BLOCKED_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "headers",
    "header",
    "credential",
    "credentials",
    "private_key",
    "access_token",
}


def sanitize_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (parameters or {}).items():
        lowered = str(key).strip().lower()
        if lowered in _BLOCKED_KEYS or any(part in lowered for part in ("secret", "password", "token")):
            continue
        if isinstance(value, str) and len(value) > 500:
            safe[key] = value[:500]
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
            continue
        if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value[:12]):
            safe[key] = value[:12]
    return safe
