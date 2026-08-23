"""Authorize registered external tools. Known ≠ enabled ≠ authorized."""

from __future__ import annotations

import re
from typing import Any

from atlas.config.settings import Settings
from atlas.domain.enums import ExternalAuthorizationMode
from atlas.domain.exceptions import (
    ExternalToolAuthorizationError,
    ExternalToolValidationError,
    UnknownExternalToolError,
)
from atlas.domain.models import ExternalInvocation, SpecialistFollowUp
from atlas.ops.external.fetch_url import assert_fetch_arguments
from atlas.ops.external.registry import (
    CAPABILITY_FETCH_URL,
    ExternalToolRegistry,
    default_external_registry,
)
from atlas.ops.external.ssrf import validate_destination
from atlas.ops.workspace import MissionWorkspace

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def authorize_external_tool(
    capability: str,
    arguments: dict[str, Any],
    workspace: MissionWorkspace,
    *,
    registry: ExternalToolRegistry | None = None,
) -> None:
    """Raise if the tool is unknown, disabled, or not authorized for this mission."""
    settings = workspace.settings
    registry = registry or default_external_registry()
    if not settings.external_tools_enabled:
        raise ExternalToolAuthorizationError("External tools are disabled")
    try:
        descriptor = registry.get(capability)
    except UnknownExternalToolError as exc:
        raise ExternalToolAuthorizationError(str(exc)) from exc
    if descriptor.authorization_mode == ExternalAuthorizationMode.DISABLED:
        raise ExternalToolAuthorizationError(f"External tool '{capability}' is disabled")
    if capability == CAPABILITY_FETCH_URL and not settings.fetch_url_enabled:
        raise ExternalToolAuthorizationError("FETCH_URL is disabled")
    if len(workspace.mission.external_invocations) >= settings.max_external_invocations:
        raise ExternalToolAuthorizationError("External invocation budget exhausted")
    if capability == CAPABILITY_FETCH_URL:
        url = assert_fetch_arguments(arguments)
        validate_destination(url, settings)


def validate_external_arguments(
    capability: str,
    arguments: dict[str, Any],
    *,
    registry: ExternalToolRegistry | None = None,
) -> dict[str, Any]:
    """Schema-validate and strip to the declared input allowlist."""
    registry = registry or default_external_registry()
    descriptor = registry.get(capability)
    extra = set(arguments) - set(descriptor.allowed_inputs)
    if extra:
        raise ExternalToolValidationError(
            f"External tool '{capability}' rejects unknown arguments: {sorted(extra)}"
        )
    missing = [name for name in descriptor.required_inputs if name not in arguments]
    if missing:
        raise ExternalToolValidationError(
            f"External tool '{capability}' is missing required arguments: {missing}"
        )
    if capability == CAPABILITY_FETCH_URL:
        url = assert_fetch_arguments(arguments)
        return {"url": url}
    return dict(arguments)


def propose_external_follow_up(workspace: MissionWorkspace) -> SpecialistFollowUp | None:
    """Local fallback: fetch only a goal URL that is already on the allowlist."""
    settings = workspace.settings
    if not settings.external_tools_enabled or not settings.fetch_url_enabled:
        return None
    if workspace.mission.dataset_profile is None:
        return None
    if not settings.fetch_allowed_domain_list:
        return None
    for url in extract_reference_urls(workspace.mission.goal):
        if _already_attempted(workspace, url):
            continue
        try:
            authorize_external_tool(CAPABILITY_FETCH_URL, {"url": url}, workspace)
        except (ExternalToolAuthorizationError, ExternalToolValidationError):
            continue
        return SpecialistFollowUp(
            capability=CAPABILITY_FETCH_URL,
            objective=f"Retrieve approved reference {url}",
            arguments={"url": url},
            reason="Mission goal cites an approved external reference",
        )
    return None


def extract_reference_urls(goal: str) -> list[str]:
    return [match.rstrip(").,;") for match in _URL_RE.findall(goal or "")]


def _already_attempted(workspace: MissionWorkspace, url: str) -> bool:
    needle = url.rstrip("/")
    for item in workspace.mission.external_invocations:
        current = (item.source_url or item.arguments.get("url") or "").rstrip("/")
        if current == needle:
            return True
    return False
