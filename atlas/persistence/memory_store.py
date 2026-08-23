"""In-memory document store used by tests. Not a cloud backend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, TypeVar

T = TypeVar("T")


class MemoryTransaction:
    """Transaction view over the in-memory store."""

    def __init__(self, store: "MemoryDocumentStore") -> None:
        self._store = store
        self._writes: list[tuple[str, str, dict[str, Any] | None]] = []

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        return self._store._read(collection, doc_id)

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._writes.append((collection, doc_id, deepcopy(data)))

    def delete(self, collection: str, doc_id: str) -> None:
        self._writes.append((collection, doc_id, None))

    def apply(self) -> None:
        for collection, doc_id, data in self._writes:
            if data is None:
                self._store._delete(collection, doc_id)
            else:
                self._store._write(collection, doc_id, data)


class MemoryDocumentStore:
    """Process-local document store that mirrors the Firestore store API."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _read(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        docs = self._data.get(collection, {})
        value = docs.get(doc_id)
        return deepcopy(value) if value is not None else None

    def _write(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._data.setdefault(collection, {})[doc_id] = deepcopy(data)

    def _delete(self, collection: str, doc_id: str) -> None:
        self._data.get(collection, {}).pop(doc_id, None)

    async def get_document(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return self._read(collection, doc_id)

    async def set_document(
        self, collection: str, doc_id: str, data: dict[str, Any]
    ) -> None:
        async with self._lock:
            self._write(collection, doc_id, data)

    async def delete_document(self, collection: str, doc_id: str) -> None:
        async with self._lock:
            self._delete(collection, doc_id)

    async def list_documents(
        self, collection: str
    ) -> list[tuple[str, dict[str, Any]]]:
        async with self._lock:
            docs = self._data.get(collection, {})
            return [(doc_id, deepcopy(data)) for doc_id, data in docs.items()]

    async def run_transaction(self, callback: Callable[[MemoryTransaction], Awaitable[T]]) -> T:
        async with self._lock:
            txn = MemoryTransaction(self)
            result = await callback(txn)
            txn.apply()
            return result
