"""Environment-based application settings."""

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvironmentMode(str, Enum):
    """Runtime environment mode."""

    LOCAL = "local"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class PlannerBackend(str, Enum):
    """Which planner backend is active."""

    ADK = "adk"
    LOCAL_FALLBACK = "local_fallback"


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
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    # Planner override (auto | adk | local)
    planner_backend: str = Field(default="auto", alias="PLANNER_BACKEND")

    # Persistence
    database_path: Path = Field(
        default=Path("data/atlas.db"), alias="ATLAS_DATABASE_PATH"
    )

    # Dataset uploads (local filesystem; replaceable with Cloud Storage)
    upload_dir: Path = Field(
        default=Path("data/uploads"), alias="ATLAS_UPLOAD_DIR"
    )
    upload_max_bytes: int = Field(
        default=10 * 1024 * 1024, alias="ATLAS_UPLOAD_MAX_BYTES"
    )

    # Workflow timing (local dev / tests)
    step_execution_delay_seconds: float = Field(
        default=0.05, alias="ATLAS_STEP_DELAY_SECONDS"
    )

    # Agent loop safety limits
    agent_max_iterations: int = Field(default=12, alias="ATLAS_AGENT_MAX_ITERATIONS")
    agent_max_tool_calls: int = Field(default=10, alias="ATLAS_AGENT_MAX_TOOL_CALLS")
    agent_max_runtime_seconds: float = Field(
        default=120.0, alias="ATLAS_AGENT_MAX_RUNTIME_SECONDS"
    )

    @property
    def resolved_planner_backend(self) -> PlannerBackend:
        """Determine which planner backend to use."""
        if self.planner_backend.lower() == "local":
            return PlannerBackend.LOCAL_FALLBACK
        if self.planner_backend.lower() == "adk":
            return PlannerBackend.ADK
        # auto: use ADK when credentials are available
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


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
