"""Explicit JSON serialization for durable mission documents."""

from __future__ import annotations

from typing import Any

from atlas.domain.models import (
    ApprovalRequest,
    DatasetRecord,
    ExperienceRecord,
    MemoryRecord,
    Mission,
    StrategyRecord,
)


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


def memory_to_document(record: MemoryRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    return {
        "memory_id": record.memory_id,
        "fingerprint": record.fingerprint,
        "type": record.type.value,
        "scope": record.scope.value,
        "scope_ref": record.scope_ref,
        "confidence": record.confidence,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "payload": payload,
    }


def document_to_memory(document: dict[str, Any]) -> MemoryRecord:
    payload = document.get("payload")
    if isinstance(payload, dict):
        return MemoryRecord.model_validate(payload)
    return MemoryRecord.model_validate(document)


def experience_to_document(record: ExperienceRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    return {
        "experience_id": record.experience_id,
        "fingerprint": record.fingerprint,
        "mission_id": record.mission_id,
        "created_at": record.created_at.isoformat(),
        "payload": payload,
    }


def document_to_experience(document: dict[str, Any]) -> ExperienceRecord:
    payload = document.get("payload")
    if isinstance(payload, dict):
        return ExperienceRecord.model_validate(payload)
    return ExperienceRecord.model_validate(document)


def strategy_to_document(record: StrategyRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    return {
        "strategy_id": record.strategy_id,
        "fingerprint": record.fingerprint,
        "mission_category": record.mission_category.value,
        "confidence": record.confidence,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "payload": payload,
    }


def document_to_strategy(document: dict[str, Any]) -> StrategyRecord:
    payload = document.get("payload")
    if isinstance(payload, dict):
        return StrategyRecord.model_validate(payload)
    return StrategyRecord.model_validate(document)


def approval_to_document(record: ApprovalRequest) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    return {
        "approval_id": record.approval_id,
        "mission_id": record.mission_id,
        "fingerprint": record.fingerprint,
        "status": record.status.value,
        "requested_at": record.requested_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "payload": payload,
    }


def document_to_approval(document: dict[str, Any]) -> ApprovalRequest:
    payload = document.get("payload")
    if isinstance(payload, dict):
        return ApprovalRequest.model_validate(payload)
    return ApprovalRequest.model_validate(document)
