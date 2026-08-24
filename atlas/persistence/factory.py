"""Repository factory."""

import logging

from atlas.config.settings import PersistenceBackend, Settings
from atlas.domain.exceptions import CloudPersistenceError
from atlas.persistence.approval_base import ApprovalRepository
from atlas.persistence.base import MissionRepository
from atlas.persistence.dataset_base import DatasetRepository
from atlas.persistence.memory_base import MemoryRepository
from atlas.persistence.learning_base import ExperienceRepository, StrategyRepository
from atlas.persistence.sqlite_approval_repository import SQLiteApprovalRepository
from atlas.persistence.sqlite_dataset_repository import SQLiteDatasetRepository
from atlas.persistence.sqlite_memory_repository import SQLiteMemoryRepository
from atlas.persistence.sqlite_learning_repository import SQLiteLearningRepository
from atlas.persistence.sqlite_repository import SQLiteMissionRepository

logger = logging.getLogger(__name__)


def create_mission_repository(settings: Settings) -> MissionRepository:
    """Create the configured mission repository."""
    if settings.resolved_persistence == PersistenceBackend.FIRESTORE:
        if not settings.firestore_configured:
            raise CloudPersistenceError(
                "Firestore persistence requires GOOGLE_CLOUD_PROJECT"
            )
        from atlas.persistence.firestore_repository import FirestoreMissionRepository
        from atlas.persistence.firestore_store import FirestoreDocumentStore

        logger.info(
            "Using Firestore mission persistence (database=%s)",
            settings.firestore_database,
        )
        store = FirestoreDocumentStore(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
        return FirestoreMissionRepository(store)
    logger.info("Using SQLite mission persistence")
    return SQLiteMissionRepository(settings.database_path)


def create_dataset_repository(settings: Settings) -> DatasetRepository:
    """Create the configured dataset metadata repository."""
    if settings.resolved_persistence == PersistenceBackend.FIRESTORE:
        if not settings.firestore_configured:
            raise CloudPersistenceError(
                "Firestore persistence requires GOOGLE_CLOUD_PROJECT"
            )
        from atlas.persistence.firestore_repository import FirestoreDatasetRepository
        from atlas.persistence.firestore_store import FirestoreDocumentStore

        logger.info(
            "Using Firestore dataset metadata persistence (database=%s)",
            settings.firestore_database,
        )
        store = FirestoreDocumentStore(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
        return FirestoreDatasetRepository(store)
    logger.info("Using SQLite dataset metadata persistence")
    return SQLiteDatasetRepository(settings.database_path)


def create_memory_repository(settings: Settings) -> MemoryRepository:
    """Create the configured memory repository. Same backend family as missions."""
    if settings.resolved_persistence == PersistenceBackend.FIRESTORE:
        if not settings.firestore_configured:
            raise CloudPersistenceError(
                "Firestore persistence requires GOOGLE_CLOUD_PROJECT"
            )
        from atlas.persistence.firestore_memory_repository import FirestoreMemoryRepository
        from atlas.persistence.firestore_store import FirestoreDocumentStore

        logger.info("Using Firestore memory persistence")
        store = FirestoreDocumentStore(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
        return FirestoreMemoryRepository(store)
    logger.info("Using SQLite memory persistence")
    return SQLiteMemoryRepository(settings.database_path)


def create_experience_repository(settings: Settings) -> ExperienceRepository:
    """Create the configured experience repository. Same backend family as missions."""
    return _create_learning_repository(settings)


def create_strategy_repository(settings: Settings) -> StrategyRepository:
    """Create the configured strategy repository. Same backend family as missions."""
    return _create_learning_repository(settings)


def _create_learning_repository(settings: Settings):
    if settings.resolved_persistence == PersistenceBackend.FIRESTORE:
        if not settings.firestore_configured:
            raise CloudPersistenceError(
                "Firestore persistence requires GOOGLE_CLOUD_PROJECT"
            )
        from atlas.persistence.firestore_learning_repository import FirestoreLearningRepository
        from atlas.persistence.firestore_store import FirestoreDocumentStore

        logger.info("Using Firestore experience/strategy persistence")
        store = FirestoreDocumentStore(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
        return FirestoreLearningRepository(store)
    logger.info("Using SQLite experience/strategy persistence")
    return SQLiteLearningRepository(settings.database_path)


def create_approval_repository(settings: Settings) -> ApprovalRepository:
    """Create the configured approval repository. Same backend family as missions."""
    if settings.resolved_persistence == PersistenceBackend.FIRESTORE:
        if not settings.firestore_configured:
            raise CloudPersistenceError(
                "Firestore persistence requires GOOGLE_CLOUD_PROJECT"
            )
        from atlas.persistence.firestore_approval_repository import FirestoreApprovalRepository
        from atlas.persistence.firestore_store import FirestoreDocumentStore

        logger.info("Using Firestore approval persistence")
        store = FirestoreDocumentStore(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
        return FirestoreApprovalRepository(store)
    logger.info("Using SQLite approval persistence")
    return SQLiteApprovalRepository(settings.database_path)
