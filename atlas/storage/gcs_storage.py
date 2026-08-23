"""Google Cloud Storage backend for dataset bytes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from atlas.domain.exceptions import CloudStorageError


class GcsDatasetStorage:
    """Stores dataset objects in a GCS bucket. Object names are generated basenames only."""

    backend_name = "gcs"

    def __init__(
        self,
        *,
        bucket_name: str,
        prefix: str = "datasets",
        project: str | None = None,
        client: object | None = None,
    ) -> None:
        if not bucket_name:
            raise CloudStorageError("ATLAS_GCS_BUCKET is required for Cloud Storage")
        self._bucket_name = bucket_name
        self._prefix = prefix.strip("/")
        self._project = project
        self._client = client

    def _object_name(self, stored_filename: str) -> str:
        if not stored_filename or stored_filename != Path(stored_filename).name:
            raise ValueError("stored_filename must be a basename with no path components")
        if ".." in stored_filename:
            raise ValueError("stored_filename must not contain '..'")
        if self._prefix:
            return f"{self._prefix}/{stored_filename}"
        return stored_filename

    def _ensure_client(self) -> object:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise CloudStorageError("google-cloud-storage is not installed") from exc
        try:
            self._client = storage.Client(project=self._project)
        except Exception as exc:
            raise CloudStorageError(f"Failed to create Cloud Storage client: {exc}") from exc
        return self._client

    def _blob(self, stored_filename: str) -> object:
        client = self._ensure_client()
        bucket = client.bucket(self._bucket_name)  # type: ignore[attr-defined]
        return bucket.blob(self._object_name(stored_filename))

    async def save(self, stored_filename: str, content: bytes) -> str:
        blob = self._blob(stored_filename)

        def _upload() -> None:
            try:
                blob.upload_from_string(content, content_type="text/csv")  # type: ignore[attr-defined]
            except Exception as exc:
                raise CloudStorageError(
                    f"Failed to upload dataset object: {exc}"
                ) from exc

        await asyncio.to_thread(_upload)
        return stored_filename

    async def load(self, stored_filename: str) -> bytes:
        blob = self._blob(stored_filename)

        def _download() -> bytes:
            try:
                exists = blob.exists()  # type: ignore[attr-defined]
                if not exists:
                    raise FileNotFoundError(
                        f"Stored dataset '{stored_filename}' was not found"
                    )
                return blob.download_as_bytes()  # type: ignore[attr-defined]
            except FileNotFoundError:
                raise
            except Exception as exc:
                raise CloudStorageError(
                    f"Failed to download dataset object: {exc}"
                ) from exc

        return await asyncio.to_thread(_download)
