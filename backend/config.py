"""Application configuration.

Every value can be overridden with an environment variable prefixed with
``ECO_`` (for example ``ECO_SAMPLE_INTERVAL_SECONDS=2``) or set in a local
``.env`` file. Defaults are chosen so that ``uvicorn backend.main:app`` works
with no configuration at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DEFAULT_DB_PATH = DATA_DIR / "ecofraction.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ECO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Eco-Fraction"
    api_prefix: str = "/api/v1"
    environment: str = "local"
    log_level: str = "INFO"

    # SQLite by default: no server, no credentials, no cost.
    database_url: str = f"sqlite:///{DEFAULT_DB_PATH}"
    sql_echo: bool = False

    # Simulated device fleet
    enable_simulator: bool = True
    sample_interval_seconds: int = Field(default=5, ge=1, le=3600)

    # History backfill so the dashboard is never empty on first run
    auto_backfill: bool = True
    backfill_days: int = Field(default=7, ge=0, le=90)
    backfill_step_minutes: int = Field(default=5, ge=1, le=60)

    # Guardrails
    max_readings_per_request: int = Field(default=5000, ge=1, le=100_000)
    max_series_points: int = Field(default=1000, ge=10, le=10_000)

    serve_frontend: bool = True
    cors_origins: list[str] = ["*"]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_memory_db(self) -> bool:
        return ":memory:" in self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
