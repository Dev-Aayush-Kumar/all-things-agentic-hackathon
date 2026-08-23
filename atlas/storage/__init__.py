"""Object storage abstractions."""

from atlas.storage.base import DatasetStorage
from atlas.storage.factory import create_dataset_storage
from atlas.storage.local_storage import LocalFileStorage

__all__ = ["DatasetStorage", "LocalFileStorage", "create_dataset_storage"]
