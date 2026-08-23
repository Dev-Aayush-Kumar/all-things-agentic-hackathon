"""Allowlisted external tools. Unknown capabilities cannot execute."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from atlas.config.settings import Settings
from atlas.domain.enums import ExternalAuthorizationMode
from atlas.domain.exceptions import UnknownExternalToolError
from atlas.domain.models import CapabilityDescription
from atlas.ops.external.fetch_url import FetchResult, fetch_url

CAPABILITY_FETCH_URL = "FETCH_URL"

ExternalExecuteFn = Callable[[dict[str, Any], Settings], Awaitable[FetchResult]]


@dataclass(frozen=True)
class ExternalToolDescriptor:
    """Contract for one registered external capability."""

    name: str
    description: str
    allowed_inputs: tuple[str, ...]
    required_inputs: tuple[str, ...]
    restrictions: tuple[str, ...]
    expected_output: str
    execute: ExternalExecuteFn = field(repr=False)
    authorization_mode: ExternalAuthorizationMode = ExternalAuthorizationMode.AUTOMATIC
    timeout_seconds_setting: str = "fetch_timeout_seconds"


class ExternalToolRegistry:
    """In-process registry. The model cannot add tools at runtime."""

    def __init__(self, descriptors: list[ExternalToolDescriptor] | None = None) -> None:
        self._tools = {item.name: item for item in (descriptors or [])}

    def get(self, name: str) -> ExternalToolDescriptor:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownExternalToolError(f"Unknown external capability '{name}'")
        return tool

    def known(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def catalog(self, settings: Settings | None = None) -> list[CapabilityDescription]:
        items: list[CapabilityDescription] = []
        for tool in self._tools.values():
            if settings is not None and not _tool_enabled(tool.name, settings):
                continue
            items.append(
                CapabilityDescription(
                    name=tool.name,
                    kind="external",
                    purpose=tool.description,
                    allowed_inputs=list(tool.allowed_inputs),
                    required_inputs=list(tool.required_inputs),
                    restrictions=list(tool.restrictions),
                    expected_output=tool.expected_output,
                )
            )
        return items


async def _execute_fetch_url(arguments: dict[str, Any], settings: Settings) -> FetchResult:
    from atlas.ops.external.fetch_url import assert_fetch_arguments

    url = assert_fetch_arguments(arguments)
    return await fetch_url(url, settings)


def _default_descriptors() -> list[ExternalToolDescriptor]:
    return [
        ExternalToolDescriptor(
            name=CAPABILITY_FETCH_URL,
            description=(
                "Retrieve a bounded text excerpt from one allowlisted HTTP(S) URL. "
                "ATLAS performs the request. The model cannot set headers, cookies, "
                "credentials, timeout, or redirect policy."
            ),
            allowed_inputs=("url",),
            required_inputs=("url",),
            restrictions=(
                "url only",
                "scheme and domain allowlists apply",
                "private/loopback/link-local destinations are rejected",
                "redirects are re-validated",
                "response size and timeout are system-controlled",
            ),
            expected_output="Structured excerpt with source_url, status_code, title",
            execute=_execute_fetch_url,
        )
    ]


def default_external_registry() -> ExternalToolRegistry:
    return ExternalToolRegistry(_default_descriptors())


def _tool_enabled(name: str, settings: Settings) -> bool:
    if not settings.external_tools_enabled:
        return False
    if name == CAPABILITY_FETCH_URL:
        return settings.fetch_url_enabled
    return False
