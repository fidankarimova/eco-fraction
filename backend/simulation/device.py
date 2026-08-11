"""Simulated metering devices.

A device turns the continuous solar model into a discrete measurement stream. The
same code path produces the historical backfill and the live tick, so the curve is
continuous and every past reading is reproducible.

When real hardware arrives, this module is what gets replaced - nothing
downstream of :class:`~backend.models.Reading` needs to change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.database import Database
from backend.models import Asset, Device, Reading
from backend.simulation.solar import SiteConfig, evaluate

logger = logging.getLogger(__name__)


def site_config_from_asset(asset: Asset) -> SiteConfig:
    return SiteConfig(
        latitude=asset.latitude,
        longitude=asset.longitude,
        dc_capacity_kw=asset.dc_capacity_kw,
        ac_capacity_kw=asset.ac_capacity_kw,
        tilt_deg=asset.tilt_deg,
        azimuth_deg=asset.azimuth_deg,
        temp_coefficient_per_c=asset.temp_coefficient_per_c,
        noct_c=asset.noct_c,
        albedo=asset.albedo,
        soiling_loss=asset.soiling_loss,
        inverter_efficiency=asset.inverter_efficiency,
        weather_seed=asset.weather_seed,
    )


def _payload_hash(device_id: str, sequence: int, recorded_at: datetime, energy_wh: float) -> str:
    """Content hash of the measurement.

    Stage 2 replaces this with a device signature over the same canonical payload
    and batches the hashes into a Merkle tree, so the field is populated now to
    keep the schema stable.
    """
    payload = json.dumps(
        {
            "device_id": device_id,
            "sequence": sequence,
            "recorded_at": recorded_at.isoformat(),
            "energy_wh": round(energy_wh, 6),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_reading(
    asset: Asset,
    device: Device,
    when: datetime,
    interval_seconds: int,
    sequence: int,
    cumulative_energy_wh: float,
) -> Reading:
    """Produce one persisted-shape reading without touching the database."""
    sample = evaluate(site_config_from_asset(asset), when, asset.utc_offset_hours)
    energy_wh = sample.ac_power_w * (interval_seconds / 3600.0)
    total = cumulative_energy_wh + energy_wh

    return Reading(
        asset_id=asset.id,
        device_id=device.id,
        recorded_at=sample.timestamp,
        sequence=sequence,
        ac_power_w=sample.ac_power_w,
        dc_power_w=sample.dc_power_w,
        poa_irradiance_w_m2=sample.poa_irradiance_w_m2,
        ghi_w_m2=sample.ghi_w_m2,
        module_temp_c=sample.module_temp_c,
        ambient_temp_c=sample.ambient_temp_c,
        interval_seconds=interval_seconds,
        energy_wh=round(energy_wh, 4),
        cumulative_energy_wh=round(total, 4),
        payload_hash=_payload_hash(device.id, sequence, sample.timestamp, energy_wh),
    )


def _device_state(session: Session, device_id: str) -> tuple[int, float, datetime | None]:
    """Return (last_sequence, cumulative_energy_wh, last_recorded_at)."""
    row = session.execute(
        select(Reading.sequence, Reading.cumulative_energy_wh, Reading.recorded_at)
        .where(Reading.device_id == device_id)
        .order_by(Reading.sequence.desc())
        .limit(1)
    ).first()
    if row is None:
        return 0, 0.0, None
    last_at = row[2]
    if last_at is not None and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return int(row[0]), float(row[1]), last_at


def backfill_history(
    db: Database,
    *,
    days: int,
    step_minutes: int,
    now: datetime | None = None,
) -> int:
    """Generate ``days`` of history at ``step_minutes`` resolution.

    Idempotent: readings already present are skipped, so re-running never
    duplicates or double-counts energy.
    """
    if days <= 0:
        return 0

    now = (now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    step = timedelta(minutes=step_minutes)
    interval_seconds = step_minutes * 60
    written = 0

    with db.session() as session:
        assets = session.scalars(select(Asset)).all()
        for asset in assets:
            for device in asset.devices:
                sequence, cumulative, last_at = _device_state(session, device.id)
                start = now - timedelta(days=days)
                if last_at is not None and last_at >= start:
                    start = last_at + step

                cursor = start
                batch: list[Reading] = []
                while cursor <= now:
                    sequence += 1
                    reading = build_reading(
                        asset=asset,
                        device=device,
                        when=cursor,
                        interval_seconds=interval_seconds,
                        sequence=sequence,
                        cumulative_energy_wh=cumulative,
                    )
                    cumulative = reading.cumulative_energy_wh
                    batch.append(reading)
                    cursor += step

                if batch:
                    session.add_all(batch)
                    session.flush()
                    written += len(batch)
                    logger.info(
                        "backfilled %d readings for device %s", len(batch), device.id
                    )

    return written


def record_live_tick(db: Database, interval_seconds: int, now: datetime | None = None) -> int:
    """Append one reading per device at the current instant."""
    now = (now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    written = 0

    with db.session() as session:
        assets = session.scalars(select(Asset)).all()
        for asset in assets:
            for device in asset.devices:
                sequence, cumulative, last_at = _device_state(session, device.id)
                if last_at is not None and now <= last_at:
                    continue

                effective_interval = interval_seconds
                if last_at is not None:
                    gap = int((now - last_at).total_seconds())
                    # Cap the gap so a long shutdown cannot invent a huge energy jump.
                    effective_interval = max(1, min(gap, interval_seconds * 4))

                reading = build_reading(
                    asset=asset,
                    device=device,
                    when=now,
                    interval_seconds=effective_interval,
                    sequence=sequence + 1,
                    cumulative_energy_wh=cumulative,
                )
                session.add(reading)
                try:
                    session.flush()
                except IntegrityError:
                    session.rollback()
                    logger.warning("duplicate reading skipped for device %s", device.id)
                    continue
                written += 1

    return written


def reading_count(db: Database) -> int:
    with db.session() as session:
        return int(session.scalar(select(func.count()).select_from(Reading)) or 0)


class SimulatorLoop:
    """Background asyncio task that appends readings at a fixed cadence."""

    def __init__(self, db: Database, interval_seconds: int) -> None:
        self._db = db
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="eco-simulator")
        logger.info("simulator started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        logger.info("simulator stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                # Blocking DB work goes to the default executor so the event loop
                # keeps serving HTTP requests.
                await asyncio.to_thread(record_live_tick, self._db, self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("simulator tick failed; continuing")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
