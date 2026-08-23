"""Repository factory."""

from atlas.config.settings import Settings
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository
from atlas.persistence.sqlite_dataset_repository import SQLiteDatasetRepository
from atlas.persistence.sqlite_repository import SQLiteMissionRepository


def create_mission_repository(settings: Settings) -> MissionRepository:
    """Create the configured mission repository."""
    return SQLiteMissionRepository(settings.database_path)


def create_dataset_repository(settings: Settings) -> DatasetRepository:
    """Create the configured dataset metadata repository."""
    return SQLiteDatasetRepository(settings.database_path)
