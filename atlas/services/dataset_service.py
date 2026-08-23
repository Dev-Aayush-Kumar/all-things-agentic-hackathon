"""Dataset upload and metadata services."""

from pathlib import PurePosixPath
from uuid import uuid4

from atlas.config.settings import Settings
from atlas.domain.exceptions import DatasetNotFoundError, DatasetValidationError
from atlas.domain.models import DatasetRecord, DatasetUploadResponse
from atlas.persistence.dataset_base import DatasetRepository
from atlas.storage.base import DatasetStorage

ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
    "application/octet-stream",
}


class DatasetService:
    """Validates, stores, and retrieves uploaded CSV datasets."""

    def __init__(
        self,
        repository: DatasetRepository,
        storage: DatasetStorage,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._settings = settings

    async def upload(
        self,
        original_filename: str | None,
        content_type: str | None,
        content: bytes,
    ) -> DatasetUploadResponse:
        filename = self._safe_original_filename(original_filename)
        self._validate(filename, content_type, content)

        dataset_id = str(uuid4())
        stored_filename = f"{dataset_id}.csv"
        await self._storage.save(stored_filename, content)
        record = DatasetRecord(
            dataset_id=dataset_id,
            original_filename=filename,
            stored_filename=stored_filename,
            content_type=content_type or "text/csv",
            size_bytes=len(content),
        )
        await self._repository.create(record)
        return DatasetUploadResponse.from_record(record)

    async def get(self, dataset_id: str) -> DatasetUploadResponse:
        record = await self._repository.get(dataset_id)
        if record is None:
            raise DatasetNotFoundError(dataset_id)
        return DatasetUploadResponse.from_record(record)

    async def get_record(self, dataset_id: str) -> DatasetRecord:
        record = await self._repository.get(dataset_id)
        if record is None:
            raise DatasetNotFoundError(dataset_id)
        return record

    def _validate(
        self,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> None:
        if not content:
            raise DatasetValidationError("Uploaded file is empty")
        if len(content) > self._settings.upload_max_bytes:
            raise DatasetValidationError(
                f"File exceeds the maximum size of {self._settings.upload_max_bytes} bytes"
            )
        if not filename.lower().endswith(".csv"):
            raise DatasetValidationError("Only CSV files are supported")
        if content_type:
            normalized = content_type.split(";")[0].strip().lower()
            if normalized and normalized not in ALLOWED_CONTENT_TYPES:
                raise DatasetValidationError(
                    f"Unsupported content type '{content_type}'. Only CSV is accepted."
                )

    @staticmethod
    def _safe_original_filename(original_filename: str | None) -> str:
        raw = (original_filename or "upload.csv").replace("\\", "/")
        name = PurePosixPath(raw).name
        if not name or name in {".", ".."}:
            return "upload.csv"
        return name
