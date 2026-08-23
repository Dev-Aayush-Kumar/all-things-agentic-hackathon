"""Repository factory."""

from atlas.config.settings import Settings
from atlas.persistence.base import MissionRepository
from atlas.persistence.sqlite_repository import SQLiteMissionRepository


def create_mission_repository(settings: Settings) -> MissionRepository:
    """Create the configured mission repository."""
    return SQLiteMissionRepository(settings.database_path)
