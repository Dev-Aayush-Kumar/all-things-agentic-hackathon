"""Cloud Storage backend tests using an in-memory fake client."""

import pytest

from atlas.storage.gcs_storage import GcsDatasetStorage


class FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def upload_from_string(self, content: bytes, content_type: str | None = None) -> None:
        self._store[self.name] = content

    def exists(self) -> bool:
        return self.name in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self.name]


class FakeBucket:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self._store = store
        self.name = name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self._store, name)


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.objects, name)


@pytest.mark.asyncio
async def test_gcs_storage_save_and_load_roundtrip() -> None:
    client = FakeClient()
    storage = GcsDatasetStorage(
        bucket_name="atlas-datasets",
        prefix="datasets",
        client=client,
    )
    key = await storage.save("abc123.csv", b"col\n1\n")
    assert key == "abc123.csv"
    assert "datasets/abc123.csv" in client.objects
    loaded = await storage.load("abc123.csv")
    assert loaded == b"col\n1\n"


@pytest.mark.asyncio
async def test_gcs_storage_rejects_path_traversal() -> None:
    storage = GcsDatasetStorage(bucket_name="atlas-datasets", client=FakeClient())
    with pytest.raises(ValueError, match="basename"):
        await storage.save("../secret.csv", b"x")
    with pytest.raises(ValueError, match="basename"):
        await storage.save("nested/path.csv", b"x")
    with pytest.raises(ValueError):
        await storage.load("..\\windows.csv")


@pytest.mark.asyncio
async def test_gcs_storage_missing_object() -> None:
    storage = GcsDatasetStorage(bucket_name="atlas-datasets", client=FakeClient())
    with pytest.raises(FileNotFoundError):
        await storage.load("missing.csv")


def test_gcs_storage_requires_bucket() -> None:
    from atlas.domain.exceptions import CloudStorageError

    with pytest.raises(CloudStorageError):
        GcsDatasetStorage(bucket_name="")
