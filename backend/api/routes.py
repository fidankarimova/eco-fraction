"""HTTP routes.

Thin handlers: validate input, call the service layer, return a schema.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import services
from backend.attacks import ATTACKS, list_attacks
from backend.config import Settings
from backend.database import Database
from backend.ledger import MICRO, LedgerError
from backend.models import SCHEMA_VERSION, AnchorBatch, ImpactRecord, Reading, RevenueEvent
from backend.schemas import (
    AssetOut,
    AssetSummary,
    AttackDefinitionOut,
    AttackResult,
    ClaimRequest,
    ClaimResult,
    HealthResponse,
    HolderView,
    LedgerSummary,
    PurchaseRequest,
    ReadingOut,
    ReadingPage,
    SeriesResponse,
    TrustReport,
)
from backend.simulation.device import record_live_tick

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
        raise HTTPException(status_code=404, detail=f"Unknown asset '{asset_id}'")
    return asset


def _asset_out(asset, device_count: int) -> AssetOut:
    payload = AssetOut.model_validate(asset).model_dump()
    payload["device_count"] = device_count
    return AssetOut(**payload)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> HealthResponse:
    total = int(session.scalar(select(func.count()).select_from(Reading)) or 0)
    return HealthResponse(
        app=settings.app_name,
        environment=settings.environment,
        server_time=datetime.now(tz=timezone.utc),
        simulator_enabled=settings.enable_simulator,
        reading_count=total,
        schema_version=SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# Assets and telemetry
# ---------------------------------------------------------------------------


@router.get("/assets", response_model=list[AssetOut], tags=["assets"])
def list_assets(session: Session = Depends(get_session)) -> list[AssetOut]:
    return [_asset_out(asset, count) for asset, count in services.list_assets(session)]


@router.get("/assets/{asset_id}", response_model=AssetOut, tags=["assets"])
def get_asset(asset_id: str, session: Session = Depends(get_session)) -> AssetOut:
    asset = _require_asset(session, asset_id)
    return _asset_out(asset, len(asset.devices))


@router.get("/assets/{asset_id}/summary", response_model=AssetSummary, tags=["telemetry"])
def asset_summary(
    asset_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> AssetSummary:
    asset = _require_asset(session, asset_id)
    return services.build_summary(
        session, asset, stale_after_seconds=max(settings.sample_interval_seconds * 4, 30)
    )


@router.get(
    "/assets/{asset_id}/readings/latest", response_model=ReadingOut, tags=["telemetry"]
)
def latest_reading(asset_id: str, session: Session = Depends(get_session)) -> ReadingOut:
    _require_asset(session, asset_id)
    reading = services.latest_reading(session, asset_id)
    if reading is None:
        raise HTTPException(status_code=404, detail="No readings recorded yet")
    return ReadingOut.model_validate(reading)


@router.get("/assets/{asset_id}/readings", response_model=ReadingPage, tags=["telemetry"])
def list_readings(
    asset_id: str,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ReadingPage:
    _require_asset(session, asset_id)
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="'start' must be earlier than 'end'")
    readings = services.readings_in_range(
        session,
        asset_id,
        start=start,
        end=end,
        limit=min(limit, settings.max_readings_per_request),
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


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@router.get("/assets/{asset_id}/trust", response_model=TrustReport, tags=["verification"])
def trust(
    asset_id: str,
    window_hours: float = Query(default=24.0, gt=0, le=24 * 30),
    session: Session = Depends(get_session),
) -> TrustReport:
    asset = _require_asset(session, asset_id)
    return TrustReport(**services.trust_report(session, asset, window_hours=window_hours))


@router.post("/assets/{asset_id}/anchor", tags=["verification"])
def anchor(asset_id: str, session: Session = Depends(get_session)) -> dict:
    """Batch un-anchored readings, publish the Merkle root and settle revenue."""
    asset = _require_asset(session, asset_id)
    batch = services.anchor_pending_readings(session, asset)
    session.commit()
    if batch is None:
        return {"anchored": False, "detail": "No un-anchored readings"}
    return {
        "anchored": True,
        "batch_id": batch.id,
        "merkle_root": batch.merkle_root,
        "anchor_reference": batch.anchor_reference,
        "anchor_target": batch.anchor_target,
        "reading_count": batch.reading_count,
        "trusted_count": batch.trusted_count,
        "verified_energy_wh": batch.verified_energy_wh,
        "rejected_energy_wh": batch.rejected_energy_wh,
    }


@router.get("/verify/reading/{reading_id}", tags=["verification"])
def verify_reading(reading_id: int, session: Session = Depends(get_session)) -> dict:
    """Public verification: prove one reading belongs to an anchored batch."""
    result = services.verify_reading_inclusion(session, reading_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail="Reading not found, or not yet anchored"
        )
    return result


@router.get("/verify/batch/{merkle_root}", tags=["verification"])
def verify_batch(merkle_root: str, session: Session = Depends(get_session)) -> dict:
    result = services.verify_by_root(session, merkle_root)
    if result is None:
        raise HTTPException(status_code=404, detail="No batch with that Merkle root")
    return result


@router.get("/assets/{asset_id}/batches", tags=["verification"])
def list_batches(
    asset_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    _require_asset(session, asset_id)
    batches = session.scalars(
        select(AnchorBatch)
        .where(AnchorBatch.asset_id == asset_id)
        .order_by(AnchorBatch.id.desc())
        .limit(limit)
    ).all()
    return {
        "asset_id": asset_id,
        "count": len(batches),
        "batches": [
            {
                "batch_id": b.id,
                "merkle_root": b.merkle_root,
                "anchor_reference": b.anchor_reference,
                "anchor_target": b.anchor_target,
                "anchored_at": b.anchored_at,
                "reading_count": b.reading_count,
                "trusted_count": b.trusted_count,
                "verified_energy_wh": b.verified_energy_wh,
                "rejected_energy_wh": b.rejected_energy_wh,
            }
            for b in batches
        ],
    }


# ---------------------------------------------------------------------------
# Adversarial demo
# ---------------------------------------------------------------------------


@router.get("/attacks", response_model=list[AttackDefinitionOut], tags=["verification"])
def available_attacks() -> list[AttackDefinitionOut]:
    return [AttackDefinitionOut(**a) for a in list_attacks()]


@router.post("/assets/{asset_id}/attacks/{attack_key}", response_model=AttackResult,
             tags=["verification"])
def inject_attack(
    asset_id: str,
    attack_key: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> AttackResult:
    """Inject a tampered reading and report whether validation caught it.

    Disabled unless ``ECO_ENABLE_ATTACK_CONSOLE`` is true, so a deployed instance
    cannot have fake readings pushed into it.
    """
    if not settings.enable_attack_console:
        raise HTTPException(status_code=403, detail="Attack console is disabled")
    _require_asset(session, asset_id)
    if attack_key not in ATTACKS:
        raise HTTPException(status_code=404, detail=f"Unknown attack '{attack_key}'")

    definition, _ = ATTACKS[attack_key]
    result = record_live_tick(
        request.app.state.db, settings.sample_interval_seconds, attack_key=attack_key
    )
    if not result["outcomes"]:
        raise HTTPException(status_code=409, detail="No device accepted the injection")

    outcome = result["outcomes"][0]
    detected = not outcome["is_trusted"]
    return AttackResult(
        attack=attack_key,
        title=definition.title,
        story=definition.story,
        detected=detected,
        trust_score=outcome["trust_score"],
        failed_checks=outcome["failed_checks"],
        reported_power_w=outcome["reported_power_w"],
        checks=outcome["checks"],
        explanation=(
            f"Rejected. A reading is trusted only if it passes every check; this "
            f"one failed {', '.join(outcome['failed_checks'])} "
            f"(quality score {outcome['trust_score']:.2f}). It contributes no energy, "
            f"no impact and no revenue."
            if detected
            else "NOT DETECTED - this reading passed validation. Investigate before "
            "presenting this attack to a jury."
        ),
    )


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


@router.get("/assets/{asset_id}/token", response_model=LedgerSummary, tags=["token"])
def token_summary(asset_id: str, session: Session = Depends(get_session)) -> LedgerSummary:
    _require_asset(session, asset_id)
    ledger = services.load_ledger(session, asset_id)
    return LedgerSummary(
        **ledger.summary(),
        minimum_investment_usdc=round(ledger.token_price_micro_usdc / MICRO, 2),
    )


@router.get(
    "/assets/{asset_id}/token/holders/{address}", response_model=HolderView, tags=["token"]
)
def holder_view(
    asset_id: str, address: str, session: Session = Depends(get_session)
) -> HolderView:
    _require_asset(session, asset_id)
    return HolderView(**services.load_ledger(session, asset_id).holder_view(address))


@router.post("/assets/{asset_id}/token/purchase", response_model=HolderView, tags=["token"])
def purchase_tokens(
    asset_id: str,
    payload: PurchaseRequest = Body(...),
    session: Session = Depends(get_session),
) -> HolderView:
    """Buy fractional tokens with **mock** USDC. No real money is involved."""
    _require_asset(session, asset_id)
    ledger = services.load_ledger(session, asset_id)
    try:
        ledger.purchase(payload.address, payload.token_count)
    except LedgerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    services.save_ledger(session, ledger)
    session.commit()
    return HolderView(**ledger.holder_view(payload.address))


@router.post("/assets/{asset_id}/token/claim", response_model=ClaimResult, tags=["token"])
def claim_revenue(
    asset_id: str, payload: ClaimRequest = Body(...), session: Session = Depends(get_session)
) -> ClaimResult:
    """Pull accrued revenue. O(1) per holder and independent of holder count."""
    _require_asset(session, asset_id)
    ledger = services.load_ledger(session, asset_id)
    try:
        amount = ledger.claim(payload.address)
    except LedgerError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    services.save_ledger(session, ledger)
    session.commit()
    return ClaimResult(
        address=payload.address,
        claimed_usdc=round(amount / MICRO, 6),
        note=(
            "Settled in mock USDC on a local ledger. Stage 3 replaces this with a "
            "RevenueVault contract on Polygon Amoy testnet using test USDC. No real "
            "funds are ever involved in this prototype."
        ),
        holder=HolderView(**ledger.holder_view(payload.address)),
    )


@router.get("/assets/{asset_id}/revenue", tags=["token"])
def revenue_history(
    asset_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    _require_asset(session, asset_id)
    events = session.scalars(
        select(RevenueEvent)
        .where(RevenueEvent.asset_id == asset_id)
        .order_by(RevenueEvent.id.desc())
        .limit(limit)
    ).all()
    return {
        "asset_id": asset_id,
        "count": len(events),
        "tariff_note": "Illustrative feed-in tariff; not a market quote.",
        "events": [
            {
                "period_start": e.period_start,
                "period_end": e.period_end,
                "verified_energy_kwh": round(e.verified_energy_wh / 1000.0, 4),
                "gross_usdc": round(e.gross_micro_usdc / MICRO, 6),
                "platform_fee_usdc": round(e.platform_fee_micro_usdc / MICRO, 6),
                "opex_usdc": round(e.opex_micro_usdc / MICRO, 6),
                "net_usdc": round(e.net_micro_usdc / MICRO, 6),
                "distributed_usdc": round(e.distributed_micro_usdc / MICRO, 6),
            }
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------


@router.get("/assets/{asset_id}/impact", tags=["impact"])
def impact_history(
    asset_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    asset = _require_asset(session, asset_id)
    records = session.scalars(
        select(ImpactRecord)
        .where(ImpactRecord.asset_id == asset_id)
        .order_by(ImpactRecord.id.desc())
        .limit(limit)
    ).all()
    totals = session.execute(
        select(
            func.coalesce(func.sum(ImpactRecord.verified_kwh), 0.0),
            func.coalesce(func.sum(ImpactRecord.co2e_avoided_kg), 0.0),
        ).where(ImpactRecord.asset_id == asset_id)
    ).one()
    return {
        "asset_id": asset_id,
        "method": "Deterministic location-based emission factor, not a model output",
        "method_version": records[0].method_version if records else None,
        "emission_factor_kg_per_kwh": asset.grid_emission_factor_kg_per_kwh,
        "emission_factor_source": asset.emission_factor_source,
        "certificate_status": asset.certificate_status,
        "total_verified_kwh": round(float(totals[0]), 4),
        "total_co2e_avoided_kg": round(float(totals[1]), 4),
        "double_counting_note": (
            "CO2 is claimable only when the environmental attribute (I-REC/GO) is "
            "retired to this platform or was never issued. Any other status is "
            "reported for information only."
        ),
        "records": [
            {
                "period_start": r.period_start,
                "period_end": r.period_end,
                "verified_kwh": r.verified_kwh,
                "rejected_kwh": r.rejected_kwh,
                "co2e_avoided_kg": r.co2e_avoided_kg,
                "certificate_status": r.certificate_status,
                "is_claimable": r.is_claimable,
                "method_version": r.method_version,
            }
            for r in records
        ],
    }
