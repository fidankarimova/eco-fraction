"""HTTP routes.

Thin handlers: validate input, call the service layer, return a schema.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import services
from backend.config import Settings
from backend.database import Database
from backend.models import Reading
from backend.schemas import (
    AssetOut,
    AssetSummary,
    HealthResponse,
    ReadingOut,
    ReadingPage,
    SeriesResponse,
)

router = APIRouter()


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_session(db: Database = Depends(get_db)) -> Iterator[Session]:
    session = db.session_factory()
    try:
        yield session
    finally:
        session.close()


def _require_asset(session: Session, asset_id: str):
    asset = services.get_asset(session, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown asset '{asset_id}'"
        )
    return asset


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> HealthResponse:
    total = int(session.scalar(select(func.count()).select_from(Reading)) or 0)
    return HealthResponse(
        app=settings.app_name,
        environment=settings.environment,
        server_time=datetime.now(tz=timezone.utc),
        simulator_enabled=settings.enable_simulator,
        reading_count=total,
    )


def _asset_out(asset, device_count: int) -> AssetOut:
    payload = AssetOut.model_validate(asset).model_dump()
    payload["device_count"] = device_count
    return AssetOut(**payload)


@router.get("/assets", response_model=list[AssetOut], tags=["assets"])
def list_assets(session: Session = Depends(get_session)) -> list[AssetOut]:
    return [
        _asset_out(asset, count) for asset, count in services.list_assets(session)
    ]


@router.get("/assets/{asset_id}", response_model=AssetOut, tags=["assets"])
def get_asset(asset_id: str, session: Session = Depends(get_session)) -> AssetOut:
    asset = _require_asset(session, asset_id)
    return _asset_out(asset, len(asset.devices))


@router.get(
    "/assets/{asset_id}/summary", response_model=AssetSummary, tags=["telemetry"]
)
def asset_summary(
    asset_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> AssetSummary:
    asset = _require_asset(session, asset_id)
    stale_after = max(settings.sample_interval_seconds * 4, 30)
    return services.build_summary(session, asset, stale_after_seconds=stale_after)


@router.get(
    "/assets/{asset_id}/readings/latest", response_model=ReadingOut, tags=["telemetry"]
)
def latest_reading(asset_id: str, session: Session = Depends(get_session)) -> ReadingOut:
    _require_asset(session, asset_id)
    reading = services.latest_reading(session, asset_id)
    if reading is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No readings recorded for this asset yet",
        )
    return ReadingOut.model_validate(reading)


@router.get("/assets/{asset_id}/readings", response_model=ReadingPage, tags=["telemetry"])
def list_readings(
    asset_id: str,
    start: datetime | None = Query(default=None, description="Inclusive ISO-8601 lower bound"),
    end: datetime | None = Query(default=None, description="Inclusive ISO-8601 upper bound"),
    limit: int = Query(default=100, ge=1),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ReadingPage:
    _require_asset(session, asset_id)
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=422, detail="'start' must be earlier than 'end'"
        )
    limit = min(limit, settings.max_readings_per_request)
    readings = services.readings_in_range(
        session,
        asset_id,
        start=start,
        end=end,
        limit=limit,
        newest_first=(order == "desc"),
    )
    return ReadingPage(
        asset_id=asset_id,
        count=len(readings),
        start=start,
        end=end,
        readings=[ReadingOut.model_validate(r) for r in readings],
    )


@router.get("/assets/{asset_id}/series", response_model=SeriesResponse, tags=["telemetry"])
def asset_series(
    asset_id: str,
    window_hours: float = Query(default=24.0, gt=0, le=24 * 90),
    bucket_minutes: float = Query(default=15.0, gt=0, le=1440),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> SeriesResponse:
    asset = _require_asset(session, asset_id)
    return services.build_series(
        session,
        asset,
        window_hours=window_hours,
        bucket_minutes=bucket_minutes,
        max_points=settings.max_series_points,
    )
