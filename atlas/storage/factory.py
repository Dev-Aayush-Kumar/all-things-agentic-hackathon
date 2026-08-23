"""Dataset storage factory."""

from atlas.config.settings import Settings
from atlas.storage.base import DatasetStorage
from atlas.storage.local_storage import LocalFileStorage


def create_dataset_storage(settings: Settings) -> DatasetStorage:
    """Create the configured dataset storage backend."""
    return LocalFileStorage(settings.upload_dir)
