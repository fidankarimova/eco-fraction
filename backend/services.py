"""Service layer.

All read and orchestration logic lives here so the route handlers stay thin and
the same functions can be unit tested without HTTP.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.anchoring import MerkleTree, MerkleProof, mock_transaction_hash, verify_proof
from backend.impact import CertificateStatus, compute_impact
from backend.ledger import MICRO, AssetLedger, Holder, revenue_from_energy
from backend.models import (
    AnchorBatch,
    Asset,
    AttackEvent,
    Device,
    ImpactRecord,
    Reading,
    RevenueEvent,
    TokenHolder,
    TokenLedger,
)
from backend.schemas import AssetSummary, SeriesPoint, SeriesResponse, SystemStatus
from backend.simulation.solar import solar_position
from backend.validation import asset_trust_summary

logger = logging.getLogger(__name__)

DEFAULT_ASSET_ID = "solar-baku-01"
DEMO_INVESTOR = "0xDEMO000000000000000000000000000000000001"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_seed_asset(session: Session) -> Asset:
    """Create the demonstration asset, its meter and its token ledger."""
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
        emission_factor_source="PLACEHOLDER - replace with a cited national factor",
        certificate_status=CertificateStatus.UNKNOWN.value,
        tariff_micro_usdc_per_kwh=60_000,  # 0.06 USDC/kWh
        platform_fee_bps=100,  # 1.00%, inside the 0.5-1.5% band in the report
        opex_bps=1_800,  # 18% operations and maintenance
        weather_seed=20260810,
    )
    asset.devices.append(
        Device(
            id="meter-baku-01-a",
            model="simulated-meter",
            firmware="0.2.0",
            sample_interval_seconds=5,
            is_simulated=True,
        )
    )
    session.add(asset)
    session.flush()

    # 2,500 tokens x 50 USD = 125,000 USD asset value, matching the 50 USD
    # minimum investment claimed in the report and presentation.
    session.add(
        TokenLedger(
            asset_id=asset.id,
            total_supply=2_500,
            token_price_micro_usdc=50 * MICRO,
        )
    )
    session.flush()
    logger.info("seeded demonstration asset %s", asset.id)
    return asset


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------


def load_ledger(session: Session, asset_id: str) -> AssetLedger:
    row = session.get(TokenLedger, asset_id)
    if row is None:
        raise LookupError(f"no token ledger for asset {asset_id}")

    ledger = AssetLedger(
        asset_id=row.asset_id,
        total_supply=row.total_supply,
        token_price_micro_usdc=row.token_price_micro_usdc,
        acc_per_token=int(row.acc_per_token),
        total_deposited_micro_usdc=row.total_deposited_micro_usdc,
        total_claimed_micro_usdc=row.total_claimed_micro_usdc,
        undistributed_micro_usdc=row.undistributed_micro_usdc,
        paused=row.paused,
        pause_reason=row.pause_reason,
    )
    holders = session.scalars(
        select(TokenHolder).where(TokenHolder.asset_id == asset_id)
    ).all()
    for holder in holders:
        ledger.holders[holder.address] = Holder(
            address=holder.address,
            balance=holder.balance,
            reward_debt=holder.reward_debt,
            accrued_micro_usdc=holder.accrued_micro_usdc,
            claimed_micro_usdc=holder.claimed_micro_usdc,
        )
    return ledger


def save_ledger(session: Session, ledger: AssetLedger) -> None:
    row = session.get(TokenLedger, ledger.asset_id)
    row.acc_per_token = str(ledger.acc_per_token)
    row.total_deposited_micro_usdc = ledger.total_deposited_micro_usdc
    row.total_claimed_micro_usdc = ledger.total_claimed_micro_usdc
    row.undistributed_micro_usdc = ledger.undistributed_micro_usdc
    row.paused = ledger.paused
    row.pause_reason = ledger.pause_reason

    existing = {
        h.address: h
        for h in session.scalars(
            select(TokenHolder).where(TokenHolder.asset_id == ledger.asset_id)
        ).all()
    }
    for address, holder in ledger.holders.items():
        record = existing.get(address)
        if record is None:
            record = TokenHolder(asset_id=ledger.asset_id, address=address)
            session.add(record)
        record.balance = holder.balance
        record.reward_debt = holder.reward_debt
        record.accrued_micro_usdc = holder.accrued_micro_usdc
        record.claimed_micro_usdc = holder.claimed_micro_usdc
    session.flush()


# ---------------------------------------------------------------------------
# Anchoring, impact and revenue
# ---------------------------------------------------------------------------


def anchor_pending_readings(
    session: Session, asset: Asset, now: datetime | None = None
) -> AnchorBatch | None:
    """Batch every un-anchored reading, Merkle-root it, and settle its revenue.

    This is the pipeline in one function, in the order that makes the claims true:
    validate -> anchor -> account for impact -> distribute revenue. Untrusted
    readings are included in the batch (so the tamper is on the permanent record)
    but contribute no energy, no impact and no revenue.
    """
    now = _as_utc(now or datetime.now(tz=timezone.utc))
    pending = list(
        session.scalars(
            select(Reading)
            .where(Reading.asset_id == asset.id, Reading.batch_id.is_(None))
            .order_by(Reading.recorded_at.asc(), Reading.id.asc())
        ).all()
    )
    if not pending:
        return None

    tree = MerkleTree([r.payload_hash for r in pending])
    trusted = [r for r in pending if r.is_trusted]
    verified_wh = sum(r.energy_wh for r in trusted)
    rejected_wh = sum(r.energy_wh for r in pending if not r.is_trusted)
    interval_start = _as_utc(pending[0].recorded_at)
    interval_end = _as_utc(pending[-1].recorded_at)

    batch = AnchorBatch(
        asset_id=asset.id,
        interval_start=interval_start,
        interval_end=interval_end,
        merkle_root=tree.root_hex,
        reading_count=len(pending),
        trusted_count=len(trusted),
        verified_energy_wh=round(verified_wh, 4),
        rejected_energy_wh=round(rejected_wh, 4),
        anchor_reference=mock_transaction_hash(
            asset.id, tree.root_hex, interval_start.isoformat()
        ),
        anchor_target="local-anchor",
        anchored_at=now,
    )
    session.add(batch)
    session.flush()

    for reading in pending:
        reading.batch_id = batch.id

    # Impact, from verified energy only.
    impact = compute_impact(
        verified_wh=verified_wh,
        rejected_wh=rejected_wh,
        emission_factor_kg_per_kwh=asset.grid_emission_factor_kg_per_kwh,
        emission_factor_source=asset.emission_factor_source,
        certificate_status=CertificateStatus(asset.certificate_status),
    )
    session.add(
        ImpactRecord(
            asset_id=asset.id,
            batch_id=batch.id,
            period_start=interval_start,
            period_end=interval_end,
            verified_kwh=round(impact.verified_kwh, 4),
            rejected_kwh=round(impact.rejected_kwh, 4),
            emission_factor_kg_per_kwh=impact.emission_factor_kg_per_kwh,
            emission_factor_source=impact.emission_factor_source,
            co2e_avoided_kg=round(impact.co2e_avoided_kg, 4),
            certificate_status=impact.certificate_status.value,
            is_claimable=impact.is_claimable,
            method_version=impact.method_version,
        )
    )

    # Revenue, from verified energy only.
    breakdown = revenue_from_energy(
        verified_wh=verified_wh,
        tariff_micro_usdc_per_kwh=asset.tariff_micro_usdc_per_kwh,
        platform_fee_bps=asset.platform_fee_bps,
        opex_bps=asset.opex_bps,
    )
    ledger = load_ledger(session, asset.id)
    distributed = 0
    if not ledger.paused:
        distributed = ledger.deposit_revenue(breakdown["net_micro_usdc"])
        save_ledger(session, ledger)

    session.add(
        RevenueEvent(
            asset_id=asset.id,
            batch_id=batch.id,
            period_start=interval_start,
            period_end=interval_end,
            verified_energy_wh=round(verified_wh, 4),
            gross_micro_usdc=breakdown["gross_micro_usdc"],
            platform_fee_micro_usdc=breakdown["platform_fee_micro_usdc"],
            opex_micro_usdc=breakdown["opex_micro_usdc"],
            net_micro_usdc=breakdown["net_micro_usdc"],
            distributed_micro_usdc=distributed,
            created_at=now,
        )
    )
    session.flush()
    return batch


def verify_reading_inclusion(session: Session, reading_id: int) -> dict | None:
    """Produce a Merkle inclusion proof and re-verify it server-side.

    This is what the public verification page calls: give it a reading, get back
    the batch it belongs to, the proof path, and an independent confirmation that
    the proof recomputes the anchored root.
    """
    reading = session.get(Reading, reading_id)
    if reading is None or reading.batch_id is None:
        return None

    batch = session.get(AnchorBatch, reading.batch_id)
    siblings = list(
        session.scalars(
            select(Reading)
            .where(Reading.batch_id == batch.id)
            .order_by(Reading.recorded_at.asc(), Reading.id.asc())
        ).all()
    )
    hashes = [r.payload_hash for r in siblings]
    index = next(i for i, r in enumerate(siblings) if r.id == reading.id)

    tree = MerkleTree(hashes)
    proof = tree.proof_for_index(index)
    verified = verify_proof(reading.payload_hash, proof) and tree.root_hex == batch.merkle_root

    return {
        "reading_id": reading.id,
        "recorded_at": _as_utc(reading.recorded_at),
        "payload_hash": reading.payload_hash,
        "signature": reading.signature,
        "trust_score": reading.trust_score,
        "is_trusted": reading.is_trusted,
        "failed_checks": [f for f in (reading.trust_flags or "").split(",") if f],
        "batch_id": batch.id,
        "merkle_root": batch.merkle_root,
        "anchor_reference": batch.anchor_reference,
        "anchor_target": batch.anchor_target,
        "anchored_at": _as_utc(batch.anchored_at),
        "proof": proof.as_dict(),
        "proof_verified": verified,
        "explanation": (
            "The proof recomputes the anchored Merkle root from this reading alone. "
            "Any change to the reading would produce a different root."
            if verified
            else "Proof verification FAILED - this reading does not belong to the anchored batch."
        ),
    }


def verify_by_root(session: Session, merkle_root: str) -> dict | None:
    batch = session.scalars(
        select(AnchorBatch).where(AnchorBatch.merkle_root == merkle_root)
    ).first()
    if batch is None:
        return None
    readings = list(
        session.scalars(
            select(Reading)
            .where(Reading.batch_id == batch.id)
            .order_by(Reading.recorded_at.asc(), Reading.id.asc())
        ).all()
    )
    recomputed = MerkleTree([r.payload_hash for r in readings]).root_hex
    revenue = session.scalars(
        select(RevenueEvent).where(RevenueEvent.batch_id == batch.id)
    ).first()
    impact = session.scalars(
        select(ImpactRecord).where(ImpactRecord.batch_id == batch.id)
    ).first()

    return {
        "batch_id": batch.id,
        "asset_id": batch.asset_id,
        "merkle_root": batch.merkle_root,
        "recomputed_root": recomputed,
        "root_matches": recomputed == batch.merkle_root,
        "reading_count": batch.reading_count,
        "trusted_count": batch.trusted_count,
        "rejected_count": batch.reading_count - batch.trusted_count,
        "verified_energy_wh": batch.verified_energy_wh,
        "rejected_energy_wh": batch.rejected_energy_wh,
        "anchor_reference": batch.anchor_reference,
        "anchor_target": batch.anchor_target,
        "anchored_at": _as_utc(batch.anchored_at),
        "interval_start": _as_utc(batch.interval_start),
        "interval_end": _as_utc(batch.interval_end),
        "revenue": {
            "gross_usdc": round(revenue.gross_micro_usdc / MICRO, 6),
            "platform_fee_usdc": round(revenue.platform_fee_micro_usdc / MICRO, 6),
            "opex_usdc": round(revenue.opex_micro_usdc / MICRO, 6),
            "net_usdc": round(revenue.net_micro_usdc / MICRO, 6),
            "distributed_usdc": round(revenue.distributed_micro_usdc / MICRO, 6),
        }
        if revenue
        else None,
        "impact": {
            "verified_kwh": impact.verified_kwh,
            "co2e_avoided_kg": impact.co2e_avoided_kg,
            "certificate_status": impact.certificate_status,
            "is_claimable": impact.is_claimable,
            "method_version": impact.method_version,
            "emission_factor_source": impact.emission_factor_source,
        }
        if impact
        else None,
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_assets(session: Session) -> list[tuple[Asset, int]]:
    assets = session.scalars(select(Asset).order_by(Asset.id)).all()
    return [
        (
            asset,
            int(
                session.scalar(
                    select(func.count())
                    .select_from(Device)
                    .where(Device.asset_id == asset.id)
                )
                or 0
            ),
        )
        for asset in assets
    ]


def get_asset(session: Session, asset_id: str) -> Asset | None:
    return session.get(Asset, asset_id)


def latest_reading(session: Session, asset_id: str) -> Reading | None:
    return session.scalars(
        select(Reading)
        .where(Reading.asset_id == asset_id)
        .order_by(Reading.recorded_at.desc(), Reading.id.desc())
        .limit(1)
    ).first()


def latest_trusted_reading(session: Session, asset_id: str) -> Reading | None:
    return session.scalars(
        select(Reading)
        .where(Reading.asset_id == asset_id, Reading.is_trusted.is_(True))
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
    return list(session.scalars(stmt.order_by(order, Reading.id.desc()).limit(limit)).all())


def local_midnight_utc(asset: Asset, now: datetime) -> datetime:
    offset = timedelta(hours=asset.utc_offset_hours)
    local_now = now + offset
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0) - offset


def trust_report(
    session: Session, asset: Asset, window_hours: float = 24.0, now: datetime | None = None
) -> dict:
    now = _as_utc(now or datetime.now(tz=timezone.utc))
    start = now - timedelta(hours=window_hours)
    readings = list(
        session.scalars(
            select(Reading).where(
                Reading.asset_id == asset.id, Reading.recorded_at >= start
            )
        ).all()
    )
    summary = asset_trust_summary(readings)

    latest_batch = session.scalars(
        select(AnchorBatch)
        .where(AnchorBatch.asset_id == asset.id)
        .order_by(AnchorBatch.id.desc())
        .limit(1)
    ).first()
    batch_count = int(
        session.scalar(
            select(func.count())
            .select_from(AnchorBatch)
            .where(AnchorBatch.asset_id == asset.id)
        )
        or 0
    )
    attacks = list(
        session.scalars(
            select(AttackEvent)
            .where(AttackEvent.asset_id == asset.id)
            .order_by(AttackEvent.id.desc())
            .limit(10)
        ).all()
    )
    total_attacks = int(
        session.scalar(
            select(func.count())
            .select_from(AttackEvent)
            .where(AttackEvent.asset_id == asset.id)
        )
        or 0
    )
    detected_attacks = int(
        session.scalar(
            select(func.count())
            .select_from(AttackEvent)
            .where(AttackEvent.asset_id == asset.id, AttackEvent.detected.is_(True))
        )
        or 0
    )

    return {
        "asset_id": asset.id,
        "window_hours": window_hours,
        **summary,
        "anchored_batch_count": batch_count,
        "latest_batch": {
            "batch_id": latest_batch.id,
            "merkle_root": latest_batch.merkle_root,
            "anchor_reference": latest_batch.anchor_reference,
            "anchor_target": latest_batch.anchor_target,
            "anchored_at": _as_utc(latest_batch.anchored_at),
            "reading_count": latest_batch.reading_count,
            "trusted_count": latest_batch.trusted_count,
            "verified_energy_wh": latest_batch.verified_energy_wh,
        }
        if latest_batch
        else None,
        "attack_total": total_attacks,
        "attack_detected": detected_attacks,
        "attack_detection_rate_percent": round(
            detected_attacks / total_attacks * 100.0, 2
        )
        if total_attacks
        else None,
        "recent_attacks": [
            {
                "attack_type": a.attack_type,
                "description": a.description,
                "detected": a.detected,
                "detected_by": [f for f in (a.detected_by or "").split(",") if f],
                "trust_score": a.trust_score,
                "created_at": _as_utc(a.created_at),
            }
            for a in attacks
        ],
    }


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
    and PostgreSQL without dialect-specific date functions. At Stage 1-2 volumes
    (tens of thousands of rows) this is comfortably fast.
    """
    now = _as_utc(now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    start = now - timedelta(hours=window_hours)

    min_bucket = (window_hours * 60.0) / max_points
    bucket_minutes = max(bucket_minutes, min_bucket)
    bucket_seconds = bucket_minutes * 60.0

    rows = session.execute(
        select(
            Reading.recorded_at,
            Reading.ac_power_w,
            Reading.energy_wh,
            Reading.poa_irradiance_w_m2,
            Reading.is_trusted,
        )
        .where(Reading.asset_id == asset.id, Reading.recorded_at >= start)
        .order_by(Reading.recorded_at.asc())
    ).all()

    start_epoch = start.timestamp()
    buckets: dict[int, dict[str, float]] = {}
    for recorded_at, power_w, energy_wh, poa, is_trusted in rows:
        recorded_at = _as_utc(recorded_at)
        index = int((recorded_at.timestamp() - start_epoch) // bucket_seconds)
        bucket = buckets.setdefault(
            index,
            {
                "power_sum": 0.0,
                "peak": 0.0,
                "energy": 0.0,
                "poa_sum": 0.0,
                "n": 0.0,
                "rejected": 0.0,
            },
        )
        if is_trusted:
            bucket["power_sum"] += power_w
            bucket["peak"] = max(bucket["peak"], power_w)
            bucket["energy"] += energy_wh
            bucket["poa_sum"] += poa
            bucket["n"] += 1.0
        else:
            bucket["rejected"] += 1.0

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
                rejected_count=int(bucket["rejected"]),
            )
        )

    return SeriesResponse(
        asset_id=asset.id,
        window_hours=window_hours,
        bucket_minutes=round(bucket_minutes, 4),
        point_count=len(points),
        total_energy_kwh=round(sum(p.energy_wh for p in points) / 1000.0, 4),
        points=points,
    )


def _classify_status(asset, reading, now, stale_after_seconds):
    if reading is None:
        return SystemStatus.NO_DATA, "No readings recorded yet", None

    recorded_at = _as_utc(reading.recorded_at)
    age = (now - recorded_at).total_seconds()

    if not reading.is_trusted:
        return (
            SystemStatus.UNTRUSTED,
            "Latest reading failed validation - distribution held",
            age,
        )
    if age > stale_after_seconds:
        return SystemStatus.OFFLINE, f"No data for {int(age)} s - meter not reporting", age

    elevation, _ = solar_position(asset.latitude, asset.longitude, now)
    if elevation <= 0.0:
        return SystemStatus.NIGHT, "Sun below horizon - no generation expected", age

    if reading.ac_power_w > asset.ac_capacity_kw * 1000.0 * 0.01:
        return SystemStatus.GENERATING, "Exporting to grid - telemetry verified", age
    return SystemStatus.STANDBY, "Daylight but output below inverter threshold", age


def build_summary(
    session: Session, asset: Asset, stale_after_seconds: float, now: datetime | None = None
) -> AssetSummary:
    now = _as_utc(now or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    reading = latest_reading(session, asset.id)
    status, detail, age = _classify_status(asset, reading, now, stale_after_seconds)
    display = reading if (reading and reading.is_trusted) else latest_trusted_reading(
        session, asset.id
    )

    day_start = local_midnight_utc(asset, now)
    today = session.execute(
        select(
            func.coalesce(func.sum(Reading.energy_wh), 0.0),
            func.coalesce(func.max(Reading.ac_power_w), 0.0),
        ).where(
            Reading.asset_id == asset.id,
            Reading.recorded_at >= day_start,
            Reading.is_trusted.is_(True),
        )
    ).one()
    energy_today_wh = float(today[0])
    peak_today_w = float(today[1])

    reading_count = int(
        session.scalar(
            select(func.count()).select_from(Reading).where(Reading.asset_id == asset.id)
        )
        or 0
    )
    rejected_count = int(
        session.scalar(
            select(func.count())
            .select_from(Reading)
            .where(Reading.asset_id == asset.id, Reading.is_trusted.is_(False))
        )
        or 0
    )
    device_count = int(
        session.scalar(
            select(func.count()).select_from(Device).where(Device.asset_id == asset.id)
        )
        or 0
    )

    total_energy_wh = float(display.cumulative_energy_wh) if display else 0.0
    elapsed_hours = max((now - day_start).total_seconds() / 3600.0, 1e-6)
    ac_capacity_w = asset.ac_capacity_kw * 1000.0
    impact = compute_impact(
        verified_wh=total_energy_wh,
        rejected_wh=0.0,
        emission_factor_kg_per_kwh=asset.grid_emission_factor_kg_per_kwh,
        emission_factor_source=asset.emission_factor_source,
        certificate_status=CertificateStatus(asset.certificate_status),
    )

    return AssetSummary(
        asset_id=asset.id,
        asset_name=asset.name,
        location_name=asset.location_name,
        status=status,
        status_detail=detail,
        server_time=now,
        last_reading_at=_as_utc(reading.recorded_at) if reading else None,
        data_age_seconds=round(age, 1) if age is not None else None,
        current_power_w=round(display.ac_power_w, 2) if display else 0.0,
        current_power_percent_of_ac_capacity=(
            round(display.ac_power_w / ac_capacity_w * 100.0, 1) if display else 0.0
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
        co2_avoided_total_kg=round(impact.co2e_avoided_kg, 2),
        co2_is_claimable=impact.is_claimable,
        certificate_status=impact.certificate_status.value,
        emission_factor_source=impact.emission_factor_source,
        module_temp_c=round(display.module_temp_c, 1) if display else None,
        ambient_temp_c=round(display.ambient_temp_c, 1) if display else None,
        poa_irradiance_w_m2=round(display.poa_irradiance_w_m2, 1) if display else None,
        device_count=device_count,
        reading_count=reading_count,
        rejected_reading_count=rejected_count,
        latest_trust_score=round(reading.trust_score, 4) if reading else 1.0,
        latest_failed_checks=[
            f for f in ((reading.trust_flags or "").split(",") if reading else []) if f
        ],
    )
