"""Firestore (or in-memory document store) approval persistence."""

from __future__ import annotations

from typing import Any

from atlas.domain.enums import ApprovalStatus
from atlas.domain.models import ApprovalRequest, utc_now
from atlas.persistence.approval_base import ApprovalRepository
from atlas.persistence.codec import approval_to_document, document_to_approval

APPROVALS = "approvals"
APPROVAL_FINGERPRINTS = "approval_fingerprints"


class FirestoreApprovalRepository(ApprovalRepository):
    """Uses the same document-store API as mission/memory Firestore persistence."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def upsert(self, record: ApprovalRequest) -> ApprovalRequest:
        pointer = await self._store.get_document(APPROVAL_FINGERPRINTS, record.fingerprint)
        if pointer and pointer.get("approval_id"):
            current = await self.get(str(pointer["approval_id"]))
            if current is not None and current.mission_id == record.mission_id:
                if (
                    current.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                    and record.status == ApprovalStatus.PENDING
                    and current.approval_id != record.approval_id
                ):
                    return current
                if record.status != ApprovalStatus.PENDING:
                    if record.approval_id != current.approval_id and current.status == record.status:
                        record.approval_id = current.approval_id
                        record.requested_at = current.requested_at
        await self._store.set_document(
            APPROVALS, record.approval_id, approval_to_document(record)
        )
        await self._store.set_document(
            APPROVAL_FINGERPRINTS,
            record.fingerprint,
            {"approval_id": record.approval_id, "fingerprint": record.fingerprint, "mission_id": record.mission_id},
        )
        return record

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        document = await self._store.get_document(APPROVALS, approval_id)
        if document is None:
            return None
        return document_to_approval(document)

    async def find_by_fingerprint(self, fingerprint: str) -> ApprovalRequest | None:
        pointer = await self._store.get_document(APPROVAL_FINGERPRINTS, fingerprint)
        if pointer is None or not pointer.get("approval_id"):
            return None
        return await self.get(str(pointer["approval_id"]))

    async def list_for_mission(self, mission_id: str) -> list[ApprovalRequest]:
        rows = await self._store.list_documents(APPROVALS)
        records = [document_to_approval(data) for _, data in rows]
        filtered = [item for item in records if item.mission_id == mission_id]
        filtered.sort(key=lambda item: item.requested_at, reverse=True)
        return filtered

    async def list_expired_pending(self) -> list[ApprovalRequest]:
        now = utc_now()
        rows = await self._store.list_documents(APPROVALS)
        records = [document_to_approval(data) for _, data in rows]
        return [
            item
            for item in records
            if item.status == ApprovalStatus.PENDING and item.expires_at <= now
        ]
