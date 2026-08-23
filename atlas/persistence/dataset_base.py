"""Dataset metadata repository interface."""

from typing import Protocol

from atlas.domain.models import DatasetRecord


class DatasetRepository(Protocol):
    """Abstract persistence for dataset metadata."""

    async def create(self, record: DatasetRecord) -> DatasetRecord:
        """Persist dataset metadata."""
        ...

    async def get(self, dataset_id: str) -> DatasetRecord | None:
        """Retrieve dataset metadata by ID."""
        ...
