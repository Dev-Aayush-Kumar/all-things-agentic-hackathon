"""Explicit JSON serialization for durable mission documents."""

from __future__ import annotations

from typing import Any

from atlas.domain.models import DatasetRecord, Mission


def mission_to_document(mission: Mission) -> dict[str, Any]:
    """Flatten a mission into an explicit Firestore-friendly document."""
    payload = mission.model_dump(mode="json")
    lease = mission.execution.lease_expires_at
    return {
        "mission_id": mission.mission_id,
        "goal": mission.goal,
        "dataset_id": mission.dataset_id,
        "status": mission.status.value,
        "execution_state": mission.execution.state.value,
        "execution_id": mission.execution.execution_id,
        "worker_id": mission.execution.worker_id,
        "lease_expires_at": lease.isoformat() if lease else None,
        "attempt_count": mission.execution.attempt_count,
        "max_attempts": mission.execution.max_attempts,
        "created_at": mission.created_at.isoformat(),
        "updated_at": mission.updated_at.isoformat(),
        "payload": payload,
    }


def document_to_mission(document: dict[str, Any]) -> Mission:
    """Rebuild a Mission from an explicit document."""
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Mission document is missing an explicit payload object")
    return Mission.model_validate(payload)


def dataset_to_document(record: DatasetRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


def document_to_dataset(document: dict[str, Any]) -> DatasetRecord:
    return DatasetRecord.model_validate(document)
