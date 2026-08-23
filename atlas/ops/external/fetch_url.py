"""Registered FETCH_URL implementation. Model supplies only a URL."""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from atlas.config.settings import Settings
from atlas.domain.exceptions import ExternalToolExecutionError, ExternalToolValidationError
from atlas.ops.external.ssrf import validate_destination

USER_AGENT = "ATLAS-ExternalFetch/0.9"
EXCERPT_FALLBACK = 800
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class FetchResult:
    source_url: str
    final_url: str
    status_code: int
    content_type: str
    title: str | None
    excerpt: str
    truncated: bool
    bytes_read: int
    retrieved_at: datetime


async def fetch_url(
    url: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> FetchResult:
    """GET an allowlisted URL. Redirects are re-validated. Body is bounded."""
    first = validate_destination(url, settings)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.fetch_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.1",
            },
        )
    try:
        return await asyncio.wait_for(
            _retrieve(first.normalized_url, settings, client),
            timeout=max(0.05, settings.fetch_timeout_seconds),
        )
    except TimeoutError as exc:
        raise ExternalToolExecutionError("FETCH_URL timed out") from exc
    finally:
        if owns_client:
            await client.aclose()


async def _retrieve(
    start_url: str,
    settings: Settings,
    client: httpx.AsyncClient,
) -> FetchResult:
    current = start_url
    hops = 0
    max_hops = max(0, settings.fetch_max_redirects)
    while True:
        validate_destination(current, settings)
        try:
            response = await client.get(current)
        except httpx.TimeoutException as exc:
            raise ExternalToolExecutionError("FETCH_URL timed out") from exc
        except httpx.HTTPError as exc:
            raise ExternalToolExecutionError(f"FETCH_URL request failed: {exc}") from exc

        if response.status_code in REDIRECT_STATUSES:
            hops += 1
            if hops > max_hops:
                raise ExternalToolExecutionError("FETCH_URL exceeded the redirect limit")
            location = response.headers.get("location")
            if not location:
                raise ExternalToolExecutionError("Redirect response is missing Location")
            current = urljoin(str(response.url), location)
            continue

        return await _normalize_response(start_url, current, response, settings)


async def _normalize_response(
    source_url: str,
    final_url: str,
    response: httpx.Response,
    settings: Settings,
) -> FetchResult:
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > settings.fetch_max_bytes:
                raise ExternalToolExecutionError("Response Content-Length exceeds the size limit")
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > settings.fetch_max_bytes:
                raise ExternalToolExecutionError("Response exceeded the configured size limit")
            chunks.append(chunk)
    except ExternalToolExecutionError:
        raise
    except httpx.HTTPError as exc:
        raise ExternalToolExecutionError(f"FETCH_URL read failed: {exc}") from exc

    raw = b"".join(chunks)
    text = _decode_body(raw, response.encoding)
    title = _extract_title(text) if "html" in content_type or "<title" in text.lower() else None
    excerpt, truncated = _excerpt(text, settings.fetch_excerpt_chars or EXCERPT_FALLBACK)
    return FetchResult(
        source_url=source_url,
        final_url=str(response.url) if response.url else final_url,
        status_code=response.status_code,
        content_type=content_type or "application/octet-stream",
        title=title,
        excerpt=excerpt,
        truncated=truncated,
        bytes_read=len(raw),
        retrieved_at=datetime.now(timezone.utc),
    )


def _decode_body(raw: bytes, encoding: str | None) -> str:
    for candidate in (encoding, "utf-8", "latin-1"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_title(text: str) -> str | None:
    match = TITLE_RE.search(text)
    if not match:
        return None
    title = WHITESPACE_RE.sub(" ", html.unescape(TAG_RE.sub("", match.group(1)))).strip()
    return title[:200] or None


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    visible = WHITESPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text))).strip()
    if len(visible) <= limit:
        return visible, False
    return visible[:limit].rstrip(), True


def assert_fetch_arguments(arguments: dict) -> str:
    """Only `url` is accepted from a model decision."""
    extra = set(arguments) - {"url"}
    if extra:
        raise ExternalToolValidationError(
            f"FETCH_URL rejects model-controlled fields: {sorted(extra)}"
        )
    url = arguments.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ExternalToolValidationError("FETCH_URL requires a string url")
    return url.strip()
