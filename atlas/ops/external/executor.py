"""Execute a registered external tool and record provenance."""

from __future__ import annotations

from typing import Any

import httpx

from atlas.domain.enums import (
    EvidenceSourceType,
    ExternalInvocationStatus,
    PlannerSource,
)
from atlas.domain.exceptions import (
    ExternalToolAuthorizationError,
    ExternalToolExecutionError,
    ExternalToolValidationError,
)
from atlas.domain.models import EvidenceRecord, ExternalInvocation, utc_now
from atlas.ops.external.fetch_url import FetchResult
from atlas.ops.external.policy import authorize_external_tool, validate_external_arguments
from atlas.ops.external.registry import (
    CAPABILITY_FETCH_URL,
    ExternalToolRegistry,
    default_external_registry,
)
from atlas.ops.workspace import MissionWorkspace


class ExternalToolExecutor:
    """ATLAS-owned executor. Never invoked directly by Gemini."""

    def __init__(
        self,
        registry: ExternalToolRegistry | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._registry = registry or default_external_registry()
        self._client = client

    async def invoke(
        self,
        workspace: MissionWorkspace,
        *,
        capability: str,
        arguments: dict[str, Any],
        reason: str,
        source: PlannerSource,
    ) -> ExternalInvocation:
        del reason, source
        try:
            safe_args = validate_external_arguments(
                capability, arguments, registry=self._registry
            )
            authorize_external_tool(
                capability, safe_args, workspace, registry=self._registry
            )
        except (
            ExternalToolAuthorizationError,
            ExternalToolValidationError,
        ) as exc:
            invocation = ExternalInvocation(
                tool_name=capability,
                arguments={"url": arguments.get("url")} if isinstance(arguments.get("url"), str) else {},
                status=ExternalInvocationStatus.REJECTED,
                source_url=arguments.get("url") if isinstance(arguments.get("url"), str) else None,
                error=str(exc),
                completed_at=utc_now(),
            )
            workspace.mission.external_invocations.append(invocation)
            return invocation

        invocation = ExternalInvocation(
            tool_name=capability,
            arguments=dict(safe_args),
            status=ExternalInvocationStatus.AUTHORIZED,
            source_url=safe_args.get("url") if isinstance(safe_args.get("url"), str) else None,
        )
        workspace.mission.external_invocations.append(invocation)
        try:
            descriptor = self._registry.get(capability)
            invocation.status = ExternalInvocationStatus.RUNNING
            if capability == CAPABILITY_FETCH_URL and self._client is not None:
                from atlas.ops.external.fetch_url import fetch_url

                result = await fetch_url(
                    safe_args["url"], workspace.settings, client=self._client
                )
            else:
                result = await descriptor.execute(safe_args, workspace.settings)
            evidence = _evidence_from_result(capability, result, invocation.invocation_id)
            workspace.mission.evidence_records.append(evidence)
            invocation.status = ExternalInvocationStatus.SUCCEEDED
            invocation.evidence_id = evidence.evidence_id
            invocation.final_url = result.final_url
            invocation.completed_at = utc_now()
            return invocation
        except (
            ExternalToolAuthorizationError,
            ExternalToolValidationError,
            ExternalToolExecutionError,
        ) as exc:
            invocation.status = (
                ExternalInvocationStatus.REJECTED
                if isinstance(exc, (ExternalToolAuthorizationError, ExternalToolValidationError))
                else ExternalInvocationStatus.FAILED
            )
            invocation.error = str(exc)
            invocation.completed_at = utc_now()
            return invocation
        except Exception as exc:
            invocation.status = ExternalInvocationStatus.FAILED
            invocation.error = f"External tool failed: {exc}"
            invocation.completed_at = utc_now()
            return invocation


def _evidence_from_result(
    tool_name: str,
    result: FetchResult,
    invocation_id: str,
) -> EvidenceRecord:
    return EvidenceRecord(
        tool_name=tool_name,
        source_type=EvidenceSourceType.EXTERNAL,
        source_url=result.source_url,
        execution_status="SUCCEEDED",
        observed_facts={
            "source_url": result.source_url,
            "final_url": result.final_url,
            "status_code": result.status_code,
            "content_type": result.content_type,
            "title": result.title,
            "excerpt": result.excerpt,
            "truncated": result.truncated,
            "bytes_read": result.bytes_read,
            "retrieved_at": result.retrieved_at.isoformat(),
            "invocation_id": invocation_id,
            "kind": "external_retrieval",
        },
    )
