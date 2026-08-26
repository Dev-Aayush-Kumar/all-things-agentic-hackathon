"""Cloud and Gemini configuration tests. No network calls."""

from atlas.agent.factory import create_investigation_reasoner, create_mission_planner
from atlas.agent.gemini import DEFAULT_GEMINI_MODEL, gemini_meets_minimum, parse_gemini_version
from atlas.agent.local_planner import LocalFallbackPlanner
from atlas.agent.local_reasoner import LocalFallbackReasoner
from atlas.config.settings import (
    DispatcherBackend,
    PersistenceBackend,
    PlannerBackend,
    Settings,
    StorageBackend,
)
from atlas.domain.exceptions import CloudDispatchNotConfiguredError, CloudPersistenceError, CloudStorageError
from atlas.execution.factory import create_dispatcher
from atlas.persistence.factory import create_mission_repository
from atlas.storage.factory import create_dataset_storage
from atlas.storage.local_storage import LocalFileStorage

import pytest


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_local_auto_selects_sqlite_local_fs_and_async() -> None:
    settings = _settings(runtime_mode="local")
    assert settings.resolved_runtime_mode.value == "local"
    assert settings.resolved_persistence == PersistenceBackend.SQLITE
    assert settings.resolved_storage == StorageBackend.LOCAL_FS
    assert settings.resolved_dispatcher == DispatcherBackend.LOCAL_ASYNC
    assert settings.planner_label == "LOCAL_DEVELOPMENT_FALLBACK"
    assert settings.gemini_transport.value == "none"


def test_cloud_auto_selects_firestore_gcs_and_pubsub() -> None:
    settings = _settings(
        runtime_mode="cloud",
        persistence_backend="auto",
        storage_backend="auto",
        dispatcher="auto",
        google_cloud_project="demo",
        gcs_bucket="atlas-datasets",
        pubsub_topic="atlas-missions",
    )
    assert settings.resolved_persistence == PersistenceBackend.FIRESTORE
    assert settings.resolved_storage == StorageBackend.GCS
    assert settings.resolved_dispatcher == DispatcherBackend.PUBSUB
    assert settings.firestore_configured is True
    assert settings.gcs_configured is True
    assert settings.pubsub_configured is True


def test_explicit_local_backends_override_cloud_mode() -> None:
    settings = _settings(
        runtime_mode="cloud",
        persistence_backend="sqlite",
        storage_backend="local",
        dispatcher="local",
    )
    assert settings.resolved_persistence == PersistenceBackend.SQLITE
    assert settings.resolved_storage == StorageBackend.LOCAL_FS
    assert settings.resolved_dispatcher == DispatcherBackend.LOCAL_ASYNC


def test_default_gemini_model_meets_hackathon_minimum() -> None:
    assert DEFAULT_GEMINI_MODEL == "gemini-3.5-flash"
    settings = _settings()
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.gemini_meets_minimum is True
    assert gemini_meets_minimum("gemini-3.5-flash") is True
    assert gemini_meets_minimum("gemini-3.5-pro") is True


def test_older_gemini_model_is_not_silently_replaced() -> None:
    settings = _settings(gemini_model="gemini-2.5-flash")
    assert settings.gemini_model == "gemini-2.5-flash"
    assert settings.gemini_meets_minimum is False
    assert parse_gemini_version("gemini-2.5-flash") == (2, 5)


def test_adk_credentials_select_real_gemini_label() -> None:
    settings = _settings(google_api_key="not-a-secret-for-tests", planner_backend="auto")
    assert settings.adk_configured is True
    assert settings.resolved_planner_backend == PlannerBackend.ADK
    assert settings.planner_label == "REAL_GEMINI_ADK"
    assert settings.gemini_transport.value == "gemini_api"


def test_vertex_transport_when_configured() -> None:
    settings = _settings(
        google_genai_use_vertexai=True,
        google_cloud_project="demo",
        planner_backend="auto",
    )
    assert settings.planner_label == "REAL_GEMINI_ADK"
    assert settings.gemini_transport.value == "vertex_ai"


def test_missing_credentials_keep_local_fallback_planner() -> None:
    settings = _settings(planner_backend="auto", google_api_key=None)
    planner = create_mission_planner(settings)
    reasoner = create_investigation_reasoner(settings)
    assert isinstance(planner, LocalFallbackPlanner)
    assert isinstance(reasoner, LocalFallbackReasoner)
    assert settings.planner_label == "LOCAL_DEVELOPMENT_FALLBACK"


def test_local_factories_do_not_require_cloud() -> None:
    settings = _settings(runtime_mode="local")
    repo = create_mission_repository(settings)
    storage = create_dataset_storage(settings)
    assert repo.__class__.__name__ == "SQLiteMissionRepository"
    assert isinstance(storage, LocalFileStorage)


def test_cloud_factories_require_configuration() -> None:
    firestore_settings = _settings(runtime_mode="cloud", persistence_backend="firestore")
    with pytest.raises(CloudPersistenceError):
        create_mission_repository(firestore_settings)

    gcs_settings = _settings(runtime_mode="cloud", storage_backend="gcs")
    with pytest.raises(CloudStorageError):
        create_dataset_storage(gcs_settings)

    pubsub_settings = _settings(runtime_mode="cloud", dispatcher="pubsub")
    with pytest.raises(CloudDispatchNotConfiguredError):
        create_dispatcher(pubsub_settings)


def test_diagnostics_never_include_api_key() -> None:
    settings = _settings(google_api_key="super-secret-key-value")
    dumped = str(settings.public_diagnostics())
    assert "super-secret-key-value" not in dumped
    assert "google_api_key" not in dumped
    assert "GOOGLE_API_KEY" not in dumped


def test_settings_import_is_not_blocked_by_agent_package_cycle() -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from atlas.config.settings import Settings; Settings(_env_file=None)",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_agent_package_still_exports_factory_helpers() -> None:
    import atlas.agent as agent
    from atlas.agent.factory import create_mission_planner

    assert agent.create_mission_planner is create_mission_planner
