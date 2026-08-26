"""Redact secrets from planner/API errors before they are stored or logged."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from atlas.domain.exceptions import ModelDecisionError

_GOOGLE_API_KEY = re.compile(r"AIza[0-9A-Za-z\-_]{10,}")
_BEARER = re.compile(r"(?i)Bearer\s+\S+")
_QUERY_SECRET = re.compile(
    r"(?i)([?&])(key|api[_-]?key|token|access_token|password|secret)=[^&\s\"']+"
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)(api[_-]?key|authorization|token|password|secret|credential)[=:]\s*['\"]?[^\s&\"']+"
)
_URL = re.compile(r"https?://[^\s\"'<>]+")

_CATEGORY_PRIORITY = (
    "timeout",
    "tls",
    "malformed_json",
    "malformed_output",
    "network",
    "dependency",
    "api_error",
    "unknown",
)


def sanitize_error_message(exc: BaseException | str, *, max_len: int = 240) -> str:
    """Return a short error string with credentials stripped."""
    text = str(exc)
    text = _GOOGLE_API_KEY.sub("[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _QUERY_SECRET.sub(r"\1\2=[REDACTED]", text)
    text = _ASSIGNED_SECRET.sub(r"\1=[REDACTED]", text)
    text = _URL.sub(_host_only_url, text)
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text or "error"


def categorize_planner_failure(exc: BaseException) -> str:
    """Safe, coarse failure category. Never includes the exception body."""
    seen = [_categorize_one(item) for item in _exception_chain(exc)]
    for preferred in _CATEGORY_PRIORITY:
        if preferred in seen:
            return preferred
    return "unknown"


def describe_planner_failure(exc: BaseException) -> dict[str, Any]:
    """Structured, secret-free planner failure diagnostics.

    Walks ``__cause__`` / ``__context__``. Never includes credentials,
    Authorization headers, or credential-bearing URLs.
    """
    chain = _exception_chain(exc)
    primary = chain[0]
    cause = chain[1] if len(chain) > 1 else None
    http_status = _first_int(chain, ("code", "status_code"))
    if http_status is None:
        http_status = _response_status(chain)
    provider_status = _first_str(chain, ("status",))
    if provider_status and str(provider_status).isdigit():
        provider_status = None
    provider_code = _provider_error_code(chain)
    provider_message = _provider_message(chain)
    category = categorize_planner_failure(exc)
    combined = " ".join(
        part
        for part in (
            sanitize_error_message(exc, max_len=400),
            provider_message or "",
            str(provider_status or ""),
            str(provider_code or ""),
        )
        if part
    )
    return {
        "failure_category": category,
        "failure_stage": _failure_stage(category, http_status, combined),
        "exception_class": type(primary).__name__,
        "exception_module": _safe_module(type(primary).__module__),
        "cause_class": type(cause).__name__ if cause is not None else None,
        "http_status": http_status,
        "provider_status": _safe_token(provider_status),
        "provider_code": _safe_token(provider_code),
        "error": sanitize_error_message(provider_message or exc),
    }


def _host_only_url(match: re.Match[str]) -> str:
    parsed = urlparse(match.group(0))
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "[URL]"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _categorize_one(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if (
        isinstance(exc, TimeoutError)
        or "timeout" in name
        or "timeout" in message
        or "timed out" in message
    ):
        return "timeout"
    if isinstance(exc, ModelDecisionError):
        if "json" in message:
            return "malformed_json"
        if "timed out" in message:
            return "timeout"
        return "malformed_output"
    if "ssl" in message or "certificate" in message or "tls" in name:
        return "tls"
    if any(
        token in message
        for token in ("connect", "network", "dns", "unreachable", "name resolution")
    ):
        return "network"
    if "import" in name or isinstance(exc, ImportError):
        return "dependency"
    if any(token in name for token in ("http", "api", "client", "google", "genai")):
        return "api_error"
    if any(token in message for token in ("http", "api", "429", "quota", "permission")):
        return "api_error"
    return "unknown"


def _failure_stage(category: str, http_status: int | None, combined: str) -> str:
    text = combined.lower()
    if category in {"timeout", "tls", "network", "dependency", "malformed_json", "malformed_output"}:
        return {
            "malformed_json": "parsing",
            "malformed_output": "parsing",
        }.get(category, category)
    if http_status in {401, 403} or any(
        token in text
        for token in ("unauthenticated", "permission_denied", "api_key_invalid", "api key not valid")
    ):
        return "authentication"
    if http_status == 404 or "not_found" in text or "not found" in text:
        return "model_lookup"
    if http_status == 429 or "resource_exhausted" in text or "quota" in text or "rate limit" in text:
        return "quota"
    if http_status == 400 or "invalid_argument" in text:
        if any(token in text for token in ("schema", "response_schema", "output_schema", "json payload")):
            return "request_schema"
        return "request_validation"
    if http_status is not None and http_status >= 500:
        return "api_error"
    return category


def _first_int(chain: list[BaseException], names: tuple[str, ...]) -> int | None:
    for exc in chain:
        for name in names:
            value = getattr(exc, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                if name == "code" and value < 100:
                    continue
                if 100 <= value <= 599:
                    return value
    return None


def _response_status(chain: list[BaseException]) -> int | None:
    for exc in chain:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return status
    return None


def _first_str(chain: list[BaseException], names: tuple[str, ...]) -> str | None:
    for exc in chain:
        for name in names:
            value = getattr(exc, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _provider_error_code(chain: list[BaseException]) -> str | None:
    for exc in chain:
        details = getattr(exc, "details", None)
        extracted = _code_from_details(details)
        if extracted:
            return extracted
    return None


def _provider_message(chain: list[BaseException]) -> str | None:
    for exc in chain:
        message = getattr(exc, "message", None)
        if isinstance(message, str) and message.strip():
            return sanitize_error_message(message)
        details = getattr(exc, "details", None)
        extracted = _message_from_details(details)
        if extracted:
            return sanitize_error_message(extracted)
    return None


def _code_from_details(details: Any) -> str | None:
    if not isinstance(details, dict):
        return None
    error = details.get("error") if isinstance(details.get("error"), dict) else details
    if not isinstance(error, dict):
        return None
    for key in ("reason", "status", "canonicalCode", "code"):
        value = error.get(key)
        if isinstance(value, str) and value.strip() and not value.isdigit():
            return value.strip()
        if isinstance(value, int) and value >= 100:
            return str(value)
    return None


def _message_from_details(details: Any) -> str | None:
    if not isinstance(details, dict):
        return None
    error = details.get("error") if isinstance(details.get("error"), dict) else details
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def _safe_module(module: str) -> str:
    return module if module.startswith(("google.", "httpx", "atlas.", "builtins")) else "hidden"


def _safe_token(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return sanitize_error_message(text, max_len=80)
