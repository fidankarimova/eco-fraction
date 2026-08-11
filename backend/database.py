"""Database engine and session management.

Deliberately thin: one :class:`Database` object owns the engine and hands out
sessions. Nothing else in the codebase touches the engine directly, so swapping
SQLite for PostgreSQL later means changing ``ECO_DATABASE_URL`` and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings
from backend.models import Base

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.engine: Engine = self._build_engine(settings)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    @staticmethod
    def _build_engine(settings: Settings) -> Engine:
        kwargs: dict = {"echo": settings.sql_echo, "future": True}

        if settings.is_sqlite:
            # check_same_thread=False: the simulator loop runs in a worker thread.
            kwargs["connect_args"] = {"check_same_thread": False}
            if settings.is_memory_db:
                # Keep one shared connection so tests see the same in-memory DB.
                kwargs["poolclass"] = StaticPool
            else:
                db_path = settings.database_url.replace("sqlite:///", "", 1)
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(settings.database_url, **kwargs)

        if settings.is_sqlite:

            @event.listens_for(engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        return engine

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        logger.info("database schema ready (%s)", self._safe_url())

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()

    def _safe_url(self) -> str:
        url = self._settings.database_url
        return url if "@" not in url else url.split("@", 1)[-1]
