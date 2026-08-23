"""Real Firestore document store using google.cloud.firestore AsyncClient."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from atlas.domain.exceptions import (
    CloudPersistenceError,
    IdempotencyConflictError,
    StaleExecutionError,
)

T = TypeVar("T")

MISSIONS = "missions"
IDEMPOTENCY = "mission_idempotency"
DATASETS = "datasets"


class FirestoreTransaction:
    """Adapter so repository code can get/set inside a Firestore transaction."""

    def __init__(self, client: Any, transaction: Any) -> None:
        self._client = client
        self._transaction = transaction

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        ref = self._client.collection(collection).document(doc_id)
        snapshot = await ref.get(transaction=self._transaction)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        return dict(data) if data is not None else None

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        ref = self._client.collection(collection).document(doc_id)
        self._transaction.set(ref, data)

    def delete(self, collection: str, doc_id: str) -> None:
        ref = self._client.collection(collection).document(doc_id)
        self._transaction.delete(ref)


class FirestoreDocumentStore:
    """Production Firestore I/O. Requires Application Default Credentials."""

    backend_name = "firestore"

    def __init__(
        self,
        *,
        project: str | None,
        database: str = "(default)",
        client: Any | None = None,
    ) -> None:
        self._project = project
        self._database = database
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud.firestore import AsyncClient
        except ImportError as exc:
            raise CloudPersistenceError(
                "google-cloud-firestore is not installed"
            ) from exc
        try:
            self._client = AsyncClient(project=self._project, database=self._database)
        except Exception as exc:
            raise CloudPersistenceError(
                f"Failed to create Firestore client: {exc}"
            ) from exc
        return self._client

    async def get_document(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        try:
            snapshot = await self._ensure_client().collection(collection).document(doc_id).get()
        except Exception as exc:
            raise CloudPersistenceError(f"Firestore get failed: {exc}") from exc
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        return dict(data) if data is not None else None

    async def set_document(
        self, collection: str, doc_id: str, data: dict[str, Any]
    ) -> None:
        try:
            await self._ensure_client().collection(collection).document(doc_id).set(data)
        except Exception as exc:
            raise CloudPersistenceError(f"Firestore set failed: {exc}") from exc

    async def delete_document(self, collection: str, doc_id: str) -> None:
        try:
            await self._ensure_client().collection(collection).document(doc_id).delete()
        except Exception as exc:
            raise CloudPersistenceError(f"Firestore delete failed: {exc}") from exc

    async def list_documents(
        self, collection: str
    ) -> list[tuple[str, dict[str, Any]]]:
        try:
            stream = self._ensure_client().collection(collection).stream()
            results: list[tuple[str, dict[str, Any]]] = []
            async for snapshot in stream:
                data = snapshot.to_dict() or {}
                results.append((snapshot.id, dict(data)))
            return results
        except Exception as exc:
            raise CloudPersistenceError(f"Firestore list failed: {exc}") from exc

    async def run_transaction(self, callback: Callable[[FirestoreTransaction], Awaitable[T]]) -> T:
        try:
            from google.cloud.firestore_v1.async_transaction import async_transactional
        except ImportError as exc:
            raise CloudPersistenceError("google-cloud-firestore is not installed") from exc

        client = self._ensure_client()

        @async_transactional
        async def _wrapped(transaction: Any) -> T:
            return await callback(FirestoreTransaction(client, transaction))

        try:
            return await _wrapped(client.transaction())
        except (CloudPersistenceError, IdempotencyConflictError, StaleExecutionError):
            raise
        except Exception as exc:
            raise CloudPersistenceError(f"Firestore transaction failed: {exc}") from exc
