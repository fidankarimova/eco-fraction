"""Service layer.

All read logic lives here so the route handlers stay thin and the same functions
can be unit tested without HTTP.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Asset, Device, Reading
from backend.schemas import (
    AssetSummary,
    SeriesPoint,
    SeriesResponse,
    SystemStatus,
)
from backend.simulation.solar import solar_position

logger = logging.getLogger(__name__)

DEFAULT_ASSET_ID = "solar-baku-01"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_seed_asset(session: Session) -> Asset:
    """Create the demonstration asset and its meter if the database is empty."""
    existing = session.get(Asset, DEFAULT_ASSET_ID)
    if existing is not None:
        return existing

    asset = Asset(
        id=DEFAULT_ASSET_ID,
        name="Bina Rooftop Array",
        location_name="Baku, Azerbaijan",
        latitude=40.4093,
        longitude=49.8671,
        utc_offset_hours=4.0,
        dc_capacity_kw=12.5,
        ac_capacity_kw=10.0,
        tilt_deg=30.0,
        azimuth_deg=180.0,
        temp_coefficient_per_c=-0.0035,
        noct_c=45.0,
        albedo=0.20,
        soiling_loss=0.02,
        inverter_efficiency=0.97,
        grid_emission_factor_kg_per_kwh=0.58,
        weather_seed=20260810,
    )
    asset.devices.append(
        Device(
            id="meter-baku-01-a",
            model="simulated-meter",
            firmware="0.1.0",
            sample_interval_seconds=5,
            is_simulated=1,
        )
    )
    session.add(asset)
    session.flush()
    logger.info("seeded demonstration asset %s", asset.id)
    return asset


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_assets(session: Session) -> list[tuple[Asset, int]]:
    assets = session.scalars(select(Asset).order_by(Asset.id)).all()
    result: list[tuple[Asset, int]] = []
    for asset in assets:
        count = int(
            session.scalar(
                select(func.count()).select_from(Device).where(Device.asset_id == asset.id)
            )
            or 0
        )
        result.append((asset, count))
    return result


def get_asset(session: Session, asset_id: str) -> Asset | None:
    return session.get(Asset, asset_id)


def latest_reading(session: Session, asset_id: str) -> Reading | None:
    return session.scalars(
        select(Reading)
        .where(Reading.asset_id == asset_id)
        .order_by(Reading.recorded_at.desc(), Reading.id.desc())
        .limit(1)
    ).first()


def readings_in_range(
    session: Session,
    asset_id: str,
    start: datetime | None,
    end: datetime | None,
    limit: int,
    newest_first: bool = True,
) -> list[Reading]:
    stmt = select(Reading).where(Reading.asset_id == asset_id)
    if start is not None:
        stmt = stmt.where(Reading.recorded_at >= start)
    if end is not None:
        stmt = stmt.where(Reading.recorded_at <= end)
    order = Reading.recorded_at.desc() if newest_first else Reading.recorded_at.asc()
    stmt = stmt.order_by(order, Reading.id.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def local_midnight_utc(asset: Asset, now: datetime) -> datetime:
    """Start of the asset's local day, expressed in UTC."""
    offset = timedelta(hours=asset.utc_offset_hours)
    local_now = now + offset
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight - offset


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def build_series(
    session: Session,
    asset: Asset,
    window_hours: float,
    bucket_minutes: float,
    max_points: int,
    now: datetime | None = None,
) -> SeriesResponse:
    """Bucket readings into fixed time slots.

    Bucketing happens in Python rather than SQL so the same code works on SQLite
    and PostgreSQL without dialect-specific date functions. At Stage 1 volumes
    (tens of thousands of rows) this is comfortably fast.
    """
    now = (now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    start = now - timedelta(hours=window_hours)

    # Widen the bucket if the requested resolution would blow past max_points.
    min_bucket = (window_hours * 60.0) / max_points
    bucket_minutes = max(bucket_minutes, min_bucket)
    bucket_seconds = bucket_minutes * 60.0

    rows = session.execute(
        select(
            Reading.recorded_at,
            Reading.ac_power_w,
            Reading.energy_wh,
            Reading.poa_irradiance_w_m2,
        )
        .where(Reading.asset_id == asset.id, Reading.recorded_at >= start)
        .order_by(Reading.recorded_at.asc())
    ).all()

    start_epoch = start.timestamp()
    buckets: dict[int, dict[str, float]] = {}
    for recorded_at, power_w, energy_wh, poa in rows:
        recorded_at = _as_utc(recorded_at)
        index = int((recorded_at.timestamp() - start_epoch) // bucket_seconds)
        bucket = buckets.setdefault(
            index,
            {"power_sum": 0.0, "peak": 0.0, "energy": 0.0, "poa_sum": 0.0, "n": 0.0},
        )
        bucket["power_sum"] += power_w
        bucket["peak"] = max(bucket["peak"], power_w)
        bucket["energy"] += energy_wh
        bucket["poa_sum"] += poa
        bucket["n"] += 1.0

    points: list[SeriesPoint] = []
    for index in sorted(buckets):
        bucket = buckets[index]
        n = max(bucket["n"], 1.0)
        points.append(
            SeriesPoint(
                bucket_start=datetime.fromtimestamp(
                    start_epoch + index * bucket_seconds, tz=timezone.utc
                ),
                average_power_w=round(bucket["power_sum"] / n, 2),
                peak_power_w=round(bucket["peak"], 2),
                energy_wh=round(bucket["energy"], 3),
                average_poa_w_m2=round(bucket["poa_sum"] / n, 2),
                sample_count=int(bucket["n"]),
            )
        )

    total_energy_wh = sum(point.energy_wh for point in points)
    return SeriesResponse(
        asset_id=asset.id,
        window_hours=window_hours,
        bucket_minutes=round(bucket_minutes, 4),
        point_count=len(points),
        total_energy_kwh=round(total_energy_wh / 1000.0, 4),
        points=points,
    )


def _classify_status(
    asset: Asset,
    reading: Reading | None,
    now: datetime,
    stale_after_seconds: float,
) -> tuple[SystemStatus, str, float | None]:
    if reading is None:
        return SystemStatus.NO_DATA, "No readings recorded yet", None

    recorded_at = _as_utc(reading.recorded_at)
    age = (now - recorded_at).total_seconds()

    if age > stale_after_seconds:
        return (
            SystemStatus.OFFLINE,
            f"No data for {int(age)} s - meter not reporting",
            age,
        )

    elevation, _ = solar_position(asset.latitude, asset.longitude, now)
    if elevation <= 0.0:
        return SystemStatus.NIGHT, "Sun below horizon - no generation expected", age

    threshold_w = asset.ac_capacity_kw * 1000.0 * 0.01
    if reading.ac_power_w > threshold_w:
        return SystemStatus.GENERATING, "Exporting to grid", age
    return SystemStatus.STANDBY, "Daylight but output below inverter threshold", age


def build_summary(
    session: Session,
    asset: Asset,
    stale_after_seconds: float,
    now: datetime | None = None,
) -> AssetSummary:
    now = (now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    reading = latest_reading(session, asset.id)
    status, detail, age = _classify_status(asset, reading, now, stale_after_seconds)

    day_start = local_midnight_utc(asset, now)
    today = session.execute(
        select(
            func.coalesce(func.sum(Reading.energy_wh), 0.0),
            func.coalesce(func.max(Reading.ac_power_w), 0.0),
        ).where(Reading.asset_id == asset.id, Reading.recorded_at >= day_start)
    ).one()
    energy_today_wh = float(today[0])
    peak_today_w = float(today[1])

    reading_count = int(
        session.scalar(
            select(func.count()).select_from(Reading).where(Reading.asset_id == asset.id)
        )
        or 0
    )
    device_count = int(
        session.scalar(
            select(func.count()).select_from(Device).where(Device.asset_id == asset.id)
        )
        or 0
    )

    total_energy_wh = float(reading.cumulative_energy_wh) if reading else 0.0
    elapsed_hours = max((now - day_start).total_seconds() / 3600.0, 1e-6)
    ac_capacity_w = asset.ac_capacity_kw * 1000.0

    return AssetSummary(
        asset_id=asset.id,
        asset_name=asset.name,
        location_name=asset.location_name,
        status=status,
        status_detail=detail,
        server_time=now,
        last_reading_at=_as_utc(reading.recorded_at) if reading else None,
        data_age_seconds=round(age, 1) if age is not None else None,
        current_power_w=round(reading.ac_power_w, 2) if reading else 0.0,
        current_power_percent_of_ac_capacity=(
            round(reading.ac_power_w / ac_capacity_w * 100.0, 1) if reading else 0.0
        ),
        ac_capacity_kw=asset.ac_capacity_kw,
        dc_capacity_kw=asset.dc_capacity_kw,
        energy_today_kwh=round(energy_today_wh / 1000.0, 3),
        energy_total_kwh=round(total_energy_wh / 1000.0, 3),
        peak_power_today_w=round(peak_today_w, 2),
        capacity_factor_today_percent=round(
            energy_today_wh / (ac_capacity_w * elapsed_hours) * 100.0, 2
        ),
        specific_yield_today_kwh_per_kwp=round(
            energy_today_wh / 1000.0 / max(asset.dc_capacity_kw, 1e-6), 3
        ),
        co2_avoided_total_kg=round(
            total_energy_wh / 1000.0 * asset.grid_emission_factor_kg_per_kwh, 2
        ),
        module_temp_c=round(reading.module_temp_c, 1) if reading else None,
        ambient_temp_c=round(reading.ambient_temp_c, 1) if reading else None,
        poa_irradiance_w_m2=round(reading.poa_irradiance_w_m2, 1) if reading else None,
        device_count=device_count,
        reading_count=reading_count,
    )
