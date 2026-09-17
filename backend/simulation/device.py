"""Simulated metering devices.

A device turns the continuous solar model into a discrete measurement stream,
**signs it at the edge**, and submits it. The server validates every submission
before it is stored - the same path a real device's MQTT publish would take.

The same code produces the historical backfill and the live tick, so the curve is
continuous and every past reading is reproducible.

When real hardware arrives, this module is what gets replaced. Nothing downstream
changes, because the contract is the signed payload, not the transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.attacks import apply_attack
from backend.crypto_util import canonical_payload, generate_keypair, sign_measurement
from backend.database import Database
from backend.models import Asset, AttackEvent, Device, Reading
from backend.simulation.solar import SiteConfig, evaluate
from backend.validation import DeviceContext, validate_measurement

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


def payload_hash(measurement: dict) -> str:
    """SHA-256 over the canonical signed payload - this is the Merkle leaf."""
    return hashlib.sha256(canonical_payload(measurement).encode("utf-8")).hexdigest()


def ensure_device_keys(session: Session) -> int:
    """Generate an Ed25519 keypair for any device that lacks one."""
    devices = session.scalars(select(Device)).all()
    created = 0
    for device in devices:
        if not device.public_key_hex:
            keypair = generate_keypair()
            device.public_key_hex = keypair.public_key_hex
            device.private_key_hex = keypair.private_key_hex
            created += 1
    if created:
        session.flush()
        logger.info("generated %d device keypair(s)", created)
    return created


def measure(
    asset: Asset, device: Device, when: datetime, interval_seconds: int, sequence: int
) -> dict:
    """Produce one honest measurement dictionary (the signed field set)."""
    sample = evaluate(site_config_from_asset(asset), when, asset.utc_offset_hours)
    return {
        "device_id": device.id,
        "asset_id": asset.id,
        "sequence": sequence,
        "recorded_at": sample.timestamp,
        "ac_power_w": sample.ac_power_w,
        "dc_power_w": sample.dc_power_w,
        "poa_irradiance_w_m2": sample.poa_irradiance_w_m2,
        "module_temp_c": sample.module_temp_c,
        "interval_seconds": interval_seconds,
        "energy_wh": round(sample.ac_power_w * (interval_seconds / 3600.0), 4),
        # Not signed, but recorded for the dashboard.
        "_ghi_w_m2": sample.ghi_w_m2,
        "_ambient_temp_c": sample.ambient_temp_c,
    }


def _signed_fields(measurement: dict) -> dict:
    return {k: v for k, v in measurement.items() if not k.startswith("_")}


def _recent_bias_ratios(
    session: Session, device_id: str, asset: Asset, attack_key: str | None
) -> list[float]:
    """Declared-vs-reference irradiance ratios for the recent readings.

    Under a sustained-drift attack the campaign has been running before this
    reading, so the history is what a real fraud would have produced: the same
    multiplier applied for a while. That history is what makes a quiet 18% bias
    detectable when a single reading cannot be.
    """
    from backend.validation import observed_reference_poa

    rows = session.execute(
        select(Reading.recorded_at, Reading.poa_irradiance_w_m2)
        .where(Reading.device_id == device_id, Reading.is_trusted.is_(True))
        .order_by(Reading.recorded_at.desc())
        .limit(12)
    ).all()

    multiplier = 1.0
    if attack_key == "scaling_drift":
        multiplier = 1.18

    ratios: list[float] = []
    for recorded_at, poa in reversed(rows):
        recorded_at = recorded_at if recorded_at.tzinfo else recorded_at.replace(
            tzinfo=timezone.utc
        )
        observed_poa, _ = observed_reference_poa(asset, recorded_at)
        if observed_poa > 50.0:
            ratios.append(poa * multiplier / observed_poa)
    return ratios


def _device_state(session: Session, device_id: str):
    """Return (last_sequence, cumulative_energy_wh, last_recorded_at, last_power)."""
    row = session.execute(
        select(
            Reading.sequence,
            Reading.cumulative_energy_wh,
            Reading.recorded_at,
            Reading.ac_power_w,
        )
        .where(Reading.device_id == device_id)
        .order_by(Reading.sequence.desc())
        .limit(1)
    ).first()
    if row is None:
        return 0, 0.0, None, None
    last_at = row[2]
    if last_at is not None and last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return int(row[0]), float(row[1]), last_at, float(row[3])


def submit_measurement(
    session: Session,
    asset: Asset,
    device: Device,
    measurement: dict,
    signature: str,
    cumulative_energy_wh: float,
    context: DeviceContext,
    now: datetime | None = None,
    injected_attack: str | None = None,
) -> tuple[Reading, object]:
    """Validate a signed measurement and persist it with its verdict.

    Untrusted readings are stored rather than discarded: rejecting silently would
    hide the attack, and the dashboard needs to show what was caught. Only trusted
    readings contribute to anchored energy, impact and revenue.
    """
    signed = _signed_fields(measurement)
    outcome = validate_measurement(signed, signature, context, asset, now=now)

    energy_wh = float(measurement["energy_wh"])
    # An untrusted reading contributes no energy to the cumulative total.
    contributed = energy_wh if outcome.is_trusted else 0.0

    reading = Reading(
        asset_id=asset.id,
        device_id=device.id,
        recorded_at=measurement["recorded_at"],
        sequence=int(measurement["sequence"]),
        ac_power_w=float(measurement["ac_power_w"]),
        dc_power_w=float(measurement["dc_power_w"]),
        poa_irradiance_w_m2=float(measurement["poa_irradiance_w_m2"]),
        ghi_w_m2=float(measurement.get("_ghi_w_m2", 0.0)),
        module_temp_c=float(measurement["module_temp_c"]),
        ambient_temp_c=float(measurement.get("_ambient_temp_c", 0.0)),
        interval_seconds=int(measurement["interval_seconds"]),
        energy_wh=round(energy_wh, 4),
        cumulative_energy_wh=round(cumulative_energy_wh + contributed, 4),
        payload_hash=payload_hash(signed),
        signature=signature,
        trust_score=outcome.trust_score,
        is_trusted=outcome.is_trusted,
        trust_flags=outcome.flags,
        injected_attack=injected_attack,
    )
    session.add(reading)
    return reading, outcome


def backfill_history(
    db: Database, *, days: int, step_minutes: int, now: datetime | None = None
) -> int:
    """Generate ``days`` of signed, validated history at ``step_minutes`` resolution.

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
        ensure_device_keys(session)
        assets = session.scalars(select(Asset)).all()
        for asset in assets:
            for device in asset.devices:
                sequence, cumulative, last_at, last_power = _device_state(
                    session, device.id
                )
                start = now - timedelta(days=days)
                if last_at is not None and last_at >= start:
                    start = last_at + step

                cursor = start
                batch = 0
                while cursor <= now:
                    sequence += 1
                    measurement = measure(
                        asset, device, cursor, interval_seconds, sequence
                    )
                    signed = _signed_fields(measurement)
                    signature = sign_measurement(device.private_key_hex, signed)
                    context = DeviceContext(
                        public_key_hex=device.public_key_hex,
                        last_sequence=sequence - 1 if sequence > 1 else None,
                        last_recorded_at=last_at,
                        last_ac_power_w=last_power,
                    )
                    # Historical readings are validated against their own instant,
                    # not wall-clock now, or every one would fail the timestamp check.
                    reading, _ = submit_measurement(
                        session,
                        asset,
                        device,
                        measurement,
                        signature,
                        cumulative,
                        context,
                        now=cursor,
                    )
                    cumulative = reading.cumulative_energy_wh
                    last_at = reading.recorded_at
                    last_power = reading.ac_power_w
                    batch += 1
                    cursor += step

                if batch:
                    session.flush()
                    written += batch
                    logger.info("backfilled %d readings for device %s", batch, device.id)

    return written


def record_live_tick(
    db: Database,
    interval_seconds: int,
    now: datetime | None = None,
    attack_key: str | None = None,
) -> dict:
    """Append one signed reading per device, optionally under attack."""
    now = (now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    result = {"written": 0, "attack": attack_key, "outcomes": []}

    with db.session() as session:
        assets = session.scalars(select(Asset)).all()
        for asset in assets:
            for device in asset.devices:
                sequence, cumulative, last_at, last_power = _device_state(
                    session, device.id
                )
                if last_at is not None and now <= last_at and attack_key is None:
                    continue

                effective_interval = interval_seconds
                if last_at is not None:
                    gap = int((now - last_at).total_seconds())
                    # Cap the gap so a long shutdown cannot invent a huge energy jump.
                    effective_interval = max(1, min(gap, interval_seconds * 4))

                bias_ratios = _recent_bias_ratios(session, device.id, asset, attack_key)
                measurement = measure(
                    asset, device, now, effective_interval, sequence + 1
                )
                signed = _signed_fields(measurement)
                definition = None

                if attack_key:
                    tampered, signature, definition = apply_attack(
                        attack_key, signed, asset, device.private_key_hex
                    )
                    measurement = {**measurement, **tampered}
                    signed = _signed_fields(measurement)
                else:
                    signature = sign_measurement(device.private_key_hex, signed)

                context = DeviceContext(
                    public_key_hex=device.public_key_hex,
                    last_sequence=sequence if sequence else None,
                    last_recorded_at=last_at,
                    last_ac_power_w=last_power,
                    recent_bias_ratios=bias_ratios,
                )
                reading, outcome = submit_measurement(
                    session,
                    asset,
                    device,
                    measurement,
                    signature,
                    cumulative,
                    context,
                    now=now,
                    injected_attack=attack_key,
                )

                verdict = {
                    "device_id": device.id,
                    "sequence": int(measurement["sequence"]),
                    "reported_power_w": float(measurement["ac_power_w"]),
                    **outcome.as_dict(),
                }

                try:
                    session.flush()
                    persisted = True
                except IntegrityError:
                    # The unique (device_id, sequence) constraint is a second line
                    # of defence behind the sequence check. A replay caught here is
                    # still a caught replay - record it rather than losing it.
                    session.rollback()
                    persisted = False
                    verdict["is_trusted"] = False
                    if "sequence" not in verdict["failed_checks"]:
                        verdict["failed_checks"].append("sequence")
                    logger.info(
                        "duplicate sequence rejected at the database for device %s",
                        device.id,
                    )

                if definition is not None:
                    session.add(
                        AttackEvent(
                            asset_id=asset.id,
                            device_id=device.id,
                            attack_type=definition.key,
                            description=definition.story,
                            detected=not verdict["is_trusted"],
                            detected_by=",".join(verdict["failed_checks"]),
                            trust_score=outcome.trust_score,
                        )
                    )

                if persisted:
                    result["written"] += 1
                result["outcomes"].append(verdict)

    return result


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
