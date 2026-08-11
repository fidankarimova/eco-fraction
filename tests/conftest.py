from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


@pytest.fixture(scope="module")
def settings() -> Settings:
    """Isolated in-memory database, simulator off, one day of history."""
    return Settings(
        database_url="sqlite:///:memory:",
        enable_simulator=False,
        auto_backfill=True,
        backfill_days=1,
        backfill_step_minutes=15,
        serve_frontend=False,
        log_level="WARNING",
        environment="test",
    )


@pytest.fixture(scope="module")
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
