"""Environment-based application settings."""

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from atlas.agent.gemini import DEFAULT_GEMINI_MODEL, gemini_meets_minimum


class EnvironmentMode(str, Enum):
    """Runtime environment mode."""

    LOCAL = "local"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class RuntimeMode(str, Enum):
    """Local development vs Google Cloud backends."""

    LOCAL = "local"
    CLOUD = "cloud"


class RuntimeRole(str, Enum):
    """Process role for the shared Cloud Run image."""

    API = "api"
    WORKER = "worker"


class PlannerBackend(str, Enum):
    """Which planner backend is active."""

    ADK = "adk"
    LOCAL_FALLBACK = "local_fallback"


class PersistenceBackend(str, Enum):
    SQLITE = "sqlite"
    FIRESTORE = "firestore"


class StorageBackend(str, Enum):
    LOCAL_FS = "local_fs"
    GCS = "gcs"


class DispatcherBackend(str, Enum):
    LOCAL_ASYNC = "local_async"
    PUBSUB = "pubsub"


class GeminiTransport(str, Enum):
    NONE = "none"
    GEMINI_API = "gemini_api"
    VERTEX_AI = "vertex_ai"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Application
    app_name: str = "ATLAS"
    environment: EnvironmentMode = EnvironmentMode.LOCAL
    debug: bool = False
    runtime_mode: str = Field(default="local", alias="ATLAS_RUNTIME_MODE")
    runtime_role: str = Field(default="api", alias="ATLAS_RUNTIME_ROLE")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Gemini / Google ADK
    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    google_genai_use_vertexai: bool = Field(
        default=False, alias="GOOGLE_GENAI_USE_VERTEXAI"
    )
    google_cloud_project: str | None = Field(default=None, alias="GOOGLE_CLOUD_PROJECT")
    google_cloud_location: str = Field(
        default="us-central1", alias="GOOGLE_CLOUD_LOCATION"
    )
    gemini_model: str = Field(default=DEFAULT_GEMINI_MODEL, alias="GEMINI_MODEL")

    # Planner override (auto | adk | gemini | local)
    planner_backend: str = Field(default="auto", alias="PLANNER_BACKEND")
    gemini_timeout_seconds: float = Field(
        default=60.0, alias="ATLAS_GEMINI_TIMEOUT_SECONDS"
    )

    # Persistence
    persistence_backend: str = Field(default="auto", alias="ATLAS_PERSISTENCE")
    database_path: Path = Field(
        default=Path("data/atlas.db"), alias="ATLAS_DATABASE_PATH"
    )
    firestore_database: str = Field(
        default="(default)", alias="ATLAS_FIRESTORE_DATABASE"
    )

    # Dataset uploads
    storage_backend: str = Field(default="auto", alias="ATLAS_STORAGE")
    upload_dir: Path = Field(
        default=Path("data/uploads"), alias="ATLAS_UPLOAD_DIR"
    )
    upload_max_bytes: int = Field(
        default=10 * 1024 * 1024, alias="ATLAS_UPLOAD_MAX_BYTES"
    )
    gcs_bucket: str | None = Field(default=None, alias="ATLAS_GCS_BUCKET")
    gcs_prefix: str = Field(default="datasets", alias="ATLAS_GCS_PREFIX")

    # Dispatch
    dispatcher: str = Field(default="auto", alias="ATLAS_DISPATCHER")
    pubsub_topic: str | None = Field(default=None, alias="ATLAS_PUBSUB_TOPIC")
    pubsub_subscription: str | None = Field(
        default=None, alias="ATLAS_PUBSUB_SUBSCRIPTION"
    )

    # Workflow timing (local dev / tests)
    step_execution_delay_seconds: float = Field(
        default=0.05, alias="ATLAS_STEP_DELAY_SECONDS"
    )

    # Agent loop safety limits
    agent_max_iterations: int = Field(default=20, alias="ATLAS_AGENT_MAX_ITERATIONS")
    agent_max_tool_calls: int = Field(default=24, alias="ATLAS_AGENT_MAX_TOOL_CALLS")
    agent_max_runtime_seconds: float = Field(
        default=120.0, alias="ATLAS_AGENT_MAX_RUNTIME_SECONDS"
    )
    specialist_task_max_attempts: int = Field(
        default=2, alias="ATLAS_TASK_MAX_ATTEMPTS"
    )
    max_mission_actions: int = Field(default=4, alias="ATLAS_MAX_ACTIONS")
    action_max_attempts: int = Field(default=2, alias="ATLAS_ACTION_MAX_ATTEMPTS")
    max_model_calls: int = Field(default=16, alias="ATLAS_MAX_MODEL_CALLS")
    max_specialist_tasks: int = Field(default=24, alias="ATLAS_MAX_SPECIALIST_TASKS")
    max_repeated_decisions: int = Field(default=2, alias="ATLAS_MAX_REPEATED_DECISIONS")
    max_external_invocations: int = Field(
        default=4, alias="ATLAS_MAX_EXTERNAL_INVOCATIONS"
    )
    external_tools_enabled: bool = Field(
        default=True, alias="ATLAS_EXTERNAL_TOOLS_ENABLED"
    )
    fetch_url_enabled: bool = Field(default=True, alias="ATLAS_FETCH_URL_ENABLED")
    fetch_allowed_domains: str = Field(
        default="", alias="ATLAS_FETCH_ALLOWED_DOMAINS"
    )
    fetch_allowed_schemes: str = Field(
        default="https,http", alias="ATLAS_FETCH_ALLOWED_SCHEMES"
    )
    fetch_timeout_seconds: float = Field(
        default=5.0, alias="ATLAS_FETCH_TIMEOUT_SECONDS"
    )
    fetch_max_bytes: int = Field(default=65536, alias="ATLAS_FETCH_MAX_BYTES")
    fetch_max_redirects: int = Field(default=3, alias="ATLAS_FETCH_MAX_REDIRECTS")
    fetch_excerpt_chars: int = Field(default=800, alias="ATLAS_FETCH_EXCERPT_CHARS")
    fetch_allow_loopback: bool = Field(
        default=False, alias="ATLAS_FETCH_ALLOW_LOOPBACK"
    )
    memory_enabled: bool = Field(default=True, alias="ATLAS_MEMORY_ENABLED")
    memory_max_retrieval: int = Field(default=5, alias="ATLAS_MEMORY_MAX_RETRIEVAL")
    memory_max_extract: int = Field(default=8, alias="ATLAS_MEMORY_MAX_EXTRACT")
    memory_min_confidence: float = Field(
        default=0.4, alias="ATLAS_MEMORY_MIN_CONFIDENCE"
    )
    memory_content_max_chars: int = Field(
        default=800, alias="ATLAS_MEMORY_CONTENT_MAX_CHARS"
    )
    strategy_enabled: bool = Field(default=True, alias="ATLAS_STRATEGY_ENABLED")
    strategy_max_retrieval: int = Field(
        default=3, alias="ATLAS_STRATEGY_MAX_RETRIEVAL"
    )
    strategy_min_confidence: float = Field(
        default=0.60, alias="ATLAS_STRATEGY_MIN_CONFIDENCE"
    )
    governance_enabled: bool = Field(default=True, alias="ATLAS_GOVERNANCE_ENABLED")
    approval_ttl_seconds: float = Field(
        default=3600.0, alias="ATLAS_APPROVAL_TTL_SECONDS"
    )

    # Durable execution
    worker_id: str | None = Field(default=None, alias="ATLAS_WORKER_ID")
    execution_lease_seconds: float = Field(
        default=30.0, alias="ATLAS_EXECUTION_LEASE_SECONDS"
    )
    execution_heartbeat_seconds: float = Field(
        default=10.0, alias="ATLAS_EXECUTION_HEARTBEAT_SECONDS"
    )
    max_execution_attempts: int = Field(
        default=3, alias="ATLAS_MAX_EXECUTION_ATTEMPTS"
    )

    @property
    def resolved_runtime_mode(self) -> RuntimeMode:
        value = self.runtime_mode.strip().lower()
        if value == "cloud":
            return RuntimeMode.CLOUD
        return RuntimeMode.LOCAL

    @property
    def resolved_runtime_role(self) -> RuntimeRole:
        value = self.runtime_role.strip().lower()
        if value == "worker":
            return RuntimeRole.WORKER
        return RuntimeRole.API

    @property
    def resolved_persistence(self) -> PersistenceBackend:
        value = self.persistence_backend.strip().lower()
        if value == "firestore":
            return PersistenceBackend.FIRESTORE
        if value == "sqlite":
            return PersistenceBackend.SQLITE
        if self.resolved_runtime_mode == RuntimeMode.CLOUD:
            return PersistenceBackend.FIRESTORE
        return PersistenceBackend.SQLITE

    @property
    def resolved_storage(self) -> StorageBackend:
        value = self.storage_backend.strip().lower()
        if value in {"gcs", "cloud_storage", "cloud-storage"}:
            return StorageBackend.GCS
        if value in {"local", "local_fs", "filesystem"}:
            return StorageBackend.LOCAL_FS
        if self.resolved_runtime_mode == RuntimeMode.CLOUD:
            return StorageBackend.GCS
        return StorageBackend.LOCAL_FS

    @property
    def resolved_dispatcher(self) -> DispatcherBackend:
        value = self.dispatcher.strip().lower()
        if value in {"pubsub", "pub_sub", "cloud"}:
            return DispatcherBackend.PUBSUB
        if value in {"local", "local_async"}:
            return DispatcherBackend.LOCAL_ASYNC
        if self.resolved_runtime_mode == RuntimeMode.CLOUD:
            return DispatcherBackend.PUBSUB
        return DispatcherBackend.LOCAL_ASYNC

    @property
    def resolved_planner_backend(self) -> PlannerBackend:
        """Determine which planner backend to use."""
        choice = self.planner_backend.lower().strip()
        if choice == "local":
            return PlannerBackend.LOCAL_FALLBACK
        if choice in {"adk", "gemini"}:
            return PlannerBackend.ADK
        if self.google_api_key or (
            self.google_genai_use_vertexai and self.google_cloud_project
        ):
            return PlannerBackend.ADK
        return PlannerBackend.LOCAL_FALLBACK

    @property
    def adk_configured(self) -> bool:
        """Whether real Gemini/ADK credentials are present."""
        if self.google_api_key:
            return True
        return bool(self.google_genai_use_vertexai and self.google_cloud_project)

    @property
    def planner_label(self) -> str:
        if (
            self.resolved_planner_backend == PlannerBackend.ADK
            and self.adk_configured
        ):
            return "REAL_GEMINI_ADK"
        return "LOCAL_DEVELOPMENT_FALLBACK"

    @property
    def gemini_transport(self) -> GeminiTransport:
        if not self.adk_configured:
            return GeminiTransport.NONE
        if self.google_genai_use_vertexai and self.google_cloud_project:
            return GeminiTransport.VERTEX_AI
        if self.google_api_key:
            return GeminiTransport.GEMINI_API
        return GeminiTransport.NONE

    @property
    def gemini_meets_minimum(self) -> bool:
        return gemini_meets_minimum(self.gemini_model)

    @property
    def pubsub_configured(self) -> bool:
        return bool(self.google_cloud_project and self.pubsub_topic)

    @property
    def firestore_configured(self) -> bool:
        return bool(self.google_cloud_project)

    @property
    def gcs_configured(self) -> bool:
        return bool(self.gcs_bucket)

    @property
    def fetch_allowed_domain_list(self) -> list[str]:
        return [
            item.strip().lower().rstrip(".")
            for item in self.fetch_allowed_domains.split(",")
            if item.strip()
        ]

    @property
    def fetch_allowed_scheme_list(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.fetch_allowed_schemes.split(",")
            if item.strip()
        ]

    def public_diagnostics(self) -> dict[str, str | bool]:
        """Runtime diagnostics safe to log or return from health endpoints."""
        return {
            "runtime_mode": self.resolved_runtime_mode.value,
            "runtime_role": self.resolved_runtime_role.value,
            "planner_label": self.planner_label,
            "planner_backend": self.resolved_planner_backend.value,
            "gemini_model": self.gemini_model,
            "gemini_transport": self.gemini_transport.value,
            "gemini_meets_minimum": self.gemini_meets_minimum,
            "persistence_backend": self.resolved_persistence.value,
            "storage_backend": self.resolved_storage.value,
            "dispatcher_backend": self.resolved_dispatcher.value,
            "adk_configured": self.adk_configured,
            "external_tools_enabled": self.external_tools_enabled,
            "fetch_url_enabled": self.fetch_url_enabled,
            "memory_enabled": self.memory_enabled,
            "strategy_enabled": self.strategy_enabled,
            "governance_enabled": self.governance_enabled,
        }

    def export_adk_runtime_env(self) -> None:
        """Copy Settings credentials into os.environ for google-genai / ADK.

        pydantic-settings can load ``GOOGLE_API_KEY`` from ``.env`` into this
        object without putting it on the process environment. google-genai only
        reads ``os.environ``. Never logs values. Does not overwrite a non-blank
        process value already present.
        """
        if self.google_api_key:
            _set_env_if_blank("GOOGLE_API_KEY", self.google_api_key)
        if self.google_genai_use_vertexai:
            _set_env_if_blank("GOOGLE_GENAI_USE_VERTEXAI", "true")
        if self.google_cloud_project:
            _set_env_if_blank("GOOGLE_CLOUD_PROJECT", self.google_cloud_project)
        if self.google_cloud_location:
            _set_env_if_blank("GOOGLE_CLOUD_LOCATION", self.google_cloud_location)


def _set_env_if_blank(name: str, value: str) -> None:
    text = str(value).strip()
    if not text:
        return
    current = os.environ.get(name)
    if current is None or not str(current).strip():
        os.environ[name] = text


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance and expose ADK credentials to the process."""
    settings = Settings()
    settings.export_adk_runtime_env()
    return settings
