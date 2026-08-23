"""Authorize, execute, and verify allowlisted actions against a working copy.

Gemini never calls this module. Specialists propose; ATLAS executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from atlas.domain.enums import ActionStatus, EventType, PlannerSource
from atlas.domain.exceptions import ActionExecutionError
from atlas.domain.models import (
    ActionRecord,
    ActionResult,
    Mission,
    MissionEvent,
    WorkingCopyState,
    WorkingCopyVersion,
    utc_now,
)
from atlas.ops.actions.registry import ActionRegistry, default_action_registry
from atlas.storage.base import DatasetStorage


@dataclass
class ActionContext:
    """Sandbox in which a single action may run."""

    mission: Mission
    agent_id: str
    storage: DatasetStorage
    frame: pd.DataFrame
    persist: Any
    task_id: str | None = None


class ActionExecutor:
    """Local action runner. Cloud workers can reuse the same contract later."""

    def __init__(self, registry: ActionRegistry | None = None) -> None:
        self._registry = registry or default_action_registry()

    @property
    def registry(self) -> ActionRegistry:
        return self._registry

    async def execute(self, record: ActionRecord, context: ActionContext) -> ActionRecord:
        spec = self._registry.authorize(record.action_type, context.agent_id)
        parameters = self._registry.validate_parameters(record.action_type, record.parameters)
        record.parameters = parameters
        record.agent_id = context.agent_id
        record.task_id = context.task_id or record.task_id
        record.provenance = PlannerSource.LOCAL_FALLBACK

        existing = _find_by_key(context.mission, record.idempotency_key)
        if existing is not None and existing is not record:
            if existing.status in {ActionStatus.VERIFIED, ActionStatus.COMPLETED}:
                return existing
            record = existing

        if record.status in {ActionStatus.VERIFIED, ActionStatus.COMPLETED}:
            return record

        if (
            record.status == ActionStatus.VERIFICATION_FAILED
            and record.attempt_count >= record.max_attempts
        ):
            raise ActionExecutionError(
                f"Action '{record.action_type}' exhausted verification retries"
                + (f": {record.error}" if record.error else "")
            )
        if record.status == ActionStatus.FAILED and record.attempt_count >= record.max_attempts:
            raise ActionExecutionError(
                f"Action '{record.action_type}' exhausted execution retries"
                + (f": {record.error}" if record.error else "")
            )

        record.status = ActionStatus.AUTHORIZED
        _event(
            context.mission,
            EventType.ACTION_AUTHORIZED,
            f"Action authorized: {record.action_type}",
            {
                "action_id": record.action_id,
                "action_type": record.action_type,
                "agent_id": context.agent_id,
                "risk": spec.risk.value,
            },
        )
        record.status = ActionStatus.RUNNING
        record.started_at = utc_now()
        record.attempt_count += 1
        record.error = None
        _event(
            context.mission,
            EventType.ACTION_STARTED,
            f"Action started: {record.action_type}",
            {
                "action_id": record.action_id,
                "action_type": record.action_type,
                "attempt": record.attempt_count,
                "input_version": record.input_version,
            },
        )
        if context.persist is not None:
            await context.persist()

        before = spec.measure(context.frame, parameters)
        try:
            after_frame = spec.transform(context.frame, parameters)
        except Exception as exc:
            record.status = ActionStatus.FAILED
            record.completed_at = utc_now()
            record.error = str(exc)
            _event(
                context.mission,
                EventType.ACTION_FAILED,
                f"Action failed: {record.action_type}",
                {
                    "action_id": record.action_id,
                    "error": str(exc),
                    "attempt": record.attempt_count,
                },
            )
            if context.persist is not None:
                await context.persist()
            raise ActionExecutionError(str(exc)) from exc

        verification = spec.verify(before, after_frame, parameters)
        record.verification = verification
        if not verification.passed:
            record.status = ActionStatus.VERIFICATION_FAILED
            record.completed_at = utc_now()
            record.error = verification.summary
            _event(
                context.mission,
                EventType.ACTION_VERIFICATION_FAILED,
                f"Action verification failed: {record.action_type}",
                {
                    "action_id": record.action_id,
                    "expected": verification.expected,
                    "actual": verification.actual,
                },
            )
            if context.persist is not None:
                await context.persist()
            raise ActionExecutionError(verification.summary)

        output_version = _next_version(context.mission)
        filename = _working_copy_filename(context.mission.mission_id, output_version)
        content = after_frame.to_csv(index=False).encode("utf-8")
        await context.storage.save(filename, content)

        working = _ensure_working_copy(context.mission, context)
        created = working.current_version == 0
        working.versions.append(
            WorkingCopyVersion(
                version=output_version,
                stored_filename=filename,
                parent_version=working.current_version or None,
                created_by_action_id=record.action_id,
                row_count=int(len(after_frame)),
                column_count=int(after_frame.shape[1]),
            )
        )
        working.current_version = output_version
        context.frame = after_frame
        record.output_version = output_version
        record.result = ActionResult(
            summary=verification.summary,
            rows_before=before.get("row_count"),
            rows_after=int(len(after_frame)),
            output_version=output_version,
            details={
                "action_type": record.action_type,
                "parameters": parameters,
                "before": before,
                "after": verification.after,
            },
        )
        record.status = ActionStatus.VERIFIED
        record.completed_at = utc_now()
        _event(
            context.mission,
            EventType.WORKING_COPY_CREATED if created else EventType.WORKING_COPY_UPDATED,
            "Working copy created" if created else "Working copy updated",
            {
                "version": output_version,
                "stored_filename": filename,
                "action_id": record.action_id,
                "row_count": int(len(after_frame)),
            },
        )
        _event(
            context.mission,
            EventType.ACTION_COMPLETED,
            f"Action completed: {record.action_type}",
            {
                "action_id": record.action_id,
                "output_version": output_version,
                "summary": verification.summary,
            },
        )
        _event(
            context.mission,
            EventType.ACTION_VERIFIED,
            f"Action verified: {record.action_type}",
            {
                "action_id": record.action_id,
                "passed": True,
                "after": verification.after,
            },
        )
        if context.persist is not None:
            await context.persist()
        return record


def _find_by_key(mission: Mission, key: str) -> ActionRecord | None:
    for item in mission.actions:
        if item.idempotency_key == key:
            return item
    return None


def _next_version(mission: Mission) -> int:
    if mission.working_copy is None:
        return 1
    return mission.working_copy.current_version + 1


def _working_copy_filename(mission_id: str, version: int) -> str:
    safe = "".join(ch for ch in mission_id if ch.isalnum() or ch in {"-", "_"})
    return f"wcopy_{safe}_v{version}.csv"


def _ensure_working_copy(mission: Mission, context: ActionContext) -> WorkingCopyState:
    if mission.working_copy is None:
        mission.working_copy = WorkingCopyState(
            source_dataset_id=mission.dataset_id or "",
            source_stored_filename="",
            source_original_filename=None,
        )
    return mission.working_copy


def _event(
    mission: Mission,
    event_type: EventType,
    message: str,
    metadata: dict | None = None,
) -> None:
    mission.events.append(
        MissionEvent(type=event_type, message=message, metadata=metadata or {})
    )
    mission.touch()
