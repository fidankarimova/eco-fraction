"""Application entrypoint.

Run with:  uvicorn backend.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from backend.api.routes import router as api_router
from backend.config import FRONTEND_DIR, Settings, get_settings
from backend.database import Database
from backend.models import SCHEMA_VERSION, Base, SchemaInfo
from backend.services import anchor_pending_readings, ensure_seed_asset
from backend.simulation.device import (
    SimulatorLoop,
    backfill_history,
    ensure_device_keys,
    reading_count,
)

logger = logging.getLogger("ecofraction")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def apply_schema(db: Database) -> None:
    """Create tables, rebuilding the prototype database on a version bump.

    This is not a migration system and does not pretend to be one. It exists so a
    developer who pulls a new stage does not hit an opaque "no such column" error
    on a throwaway SQLite file.
    """
    db.create_all()
    with db.session() as session:
        info = session.scalars(select(SchemaInfo).limit(1)).first()
        if info is None:
            session.add(SchemaInfo(id=1, version=SCHEMA_VERSION))
            return
        if info.version == SCHEMA_VERSION:
            return
        logger.warning(
            "schema version %s found, this build needs %s - rebuilding the local "
            "prototype database",
            info.version,
            SCHEMA_VERSION,
        )

    Base.metadata.drop_all(db.engine)
    db.create_all()
    with db.session() as session:
        session.add(SchemaInfo(id=1, version=SCHEMA_VERSION))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db: Database = app.state.db
        apply_schema(db)

        with db.session() as session:
            ensure_seed_asset(session)
            ensure_device_keys(session)

        if settings.auto_backfill and reading_count(db) == 0:
            logger.info(
                "empty database - backfilling %d day(s) at %d-minute resolution",
                settings.backfill_days,
                settings.backfill_step_minutes,
            )
            written = backfill_history(
                db,
                days=settings.backfill_days,
                step_minutes=settings.backfill_step_minutes,
            )
            logger.info("backfill complete: %d readings", written)

            if settings.auto_anchor:
                with db.session() as session:
                    from backend.models import Asset

                    for asset in session.scalars(select(Asset)).all():
                        batch = anchor_pending_readings(session, asset)
                        if batch is not None:
                            logger.info(
                                "anchored batch %s (root %s...) over %d readings",
                                batch.id,
                                batch.merkle_root[:16],
                                batch.reading_count,
                            )

        simulator: SimulatorLoop | None = None
        if settings.enable_simulator:
            simulator = SimulatorLoop(db, settings.sample_interval_seconds)
            await simulator.start()
        app.state.simulator = simulator

        try:
            yield
        finally:
            if simulator is not None:
                await simulator.stop()
            db.dispose()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.2.0",
        summary="Verifiable generation telemetry and fractional revenue distribution",
        description=(
            "Signed IoT telemetry, a seven-check validation engine, Merkle batch "
            "anchoring, deterministic ESG accounting and pull-based fractional "
            "revenue distribution.\n\n"
            "Everything runs locally on free and open-source components. No cloud "
            "account, no API key, no payment method, and no real funds anywhere."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.state.settings = settings
    app.state.db = Database(settings)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request, exc: Exception):  # noqa: ANN001
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Check the server log."},
        )

    # Mounted last so API routes always win.
    if settings.serve_frontend and FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()
