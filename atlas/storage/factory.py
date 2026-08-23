"""Dataset storage factory."""

import logging

from atlas.config.settings import Settings, StorageBackend
from atlas.domain.exceptions import CloudStorageError
from atlas.storage.base import DatasetStorage
from atlas.storage.local_storage import LocalFileStorage

logger = logging.getLogger(__name__)


def create_dataset_storage(settings: Settings) -> DatasetStorage:
    """Create the configured dataset storage backend."""
    if settings.resolved_storage == StorageBackend.GCS:
        if not settings.gcs_configured:
            raise CloudStorageError(
                "Cloud Storage requires ATLAS_GCS_BUCKET"
            )
        from atlas.storage.gcs_storage import GcsDatasetStorage

        assert settings.gcs_bucket is not None
        logger.info("Using Cloud Storage dataset backend (bucket configured)")
        return GcsDatasetStorage(
            bucket_name=settings.gcs_bucket,
            prefix=settings.gcs_prefix,
            project=settings.google_cloud_project,
        )
    logger.info("Using local filesystem dataset storage")
    return LocalFileStorage(settings.upload_dir)
