"""Persistence models.

Stage 1 covered assets, devices and readings. Stage 2 adds the verification and
tokenisation layers described in the submitted report:

* device public keys, so a reading is attributable
* per-reading trust score and failed-check flags
* ``anchor_batches`` - Merkle roots over validated readings
* ``token_ledgers`` / ``token_holders`` - fractional ownership and accrual
* ``revenue_events`` - energy converted to distributable revenue
* ``impact_records`` - deterministic ESG accounting with certificate status
* ``attack_events`` - audit trail for the adversarial demo

``SCHEMA_VERSION`` is checked at startup; a prototype database from an older
version is rebuilt automatically rather than failing with a confusing SQL error.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SCHEMA_VERSION = 2


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class UtcDateTime(TypeDecorator):
    """Always store and return timezone-aware UTC datetimes.

    SQLite has no native timestamp type and silently discards the offset, so a
    naive value would come back out and be parsed as browser-local time by the
    dashboard - shifting the whole chart. Normalising here fixes it once, for
    every consumer, instead of patching each response model.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class SchemaInfo(Base):
    __tablename__ = "schema_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    location_name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    utc_offset_hours: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    dc_capacity_kw: Mapped[float] = mapped_column(Float, nullable=False)
    ac_capacity_kw: Mapped[float] = mapped_column(Float, nullable=False)
    tilt_deg: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    azimuth_deg: Mapped[float] = mapped_column(Float, nullable=False, default=180.0)

    temp_coefficient_per_c: Mapped[float] = mapped_column(Float, default=-0.0035)
    noct_c: Mapped[float] = mapped_column(Float, default=45.0)
    albedo: Mapped[float] = mapped_column(Float, default=0.20)
    soiling_loss: Mapped[float] = mapped_column(Float, default=0.02)
    inverter_efficiency: Mapped[float] = mapped_column(Float, default=0.97)

    # Placeholder value. Before any impact figure is published it must be replaced
    # with a cited national grid emission factor - see README "Known gaps".
    grid_emission_factor_kg_per_kwh: Mapped[float] = mapped_column(Float, default=0.58)
    emission_factor_source: Mapped[str] = mapped_column(
        String(200), default="PLACEHOLDER - replace with a cited national factor"
    )
    certificate_status: Mapped[str] = mapped_column(String(40), default="unknown")

    # Feed-in tariff used to turn verified energy into revenue, in micro-USDC/kWh.
    tariff_micro_usdc_per_kwh: Mapped[int] = mapped_column(Integer, default=60_000)
    platform_fee_bps: Mapped[int] = mapped_column(Integer, default=100)  # 1.00%
    opex_bps: Mapped[int] = mapped_column(Integer, default=1_800)  # 18% O&M

    weather_seed: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    devices: Mapped[list["Device"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String(120), default="simulated-meter")
    firmware: Mapped[str] = mapped_column(String(40), default="0.2.0")
    sample_interval_seconds: Mapped[int] = mapped_column(Integer, default=5)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)

    # The server needs only the public key. The private key is stored here solely
    # because the "device" is a simulation running in the same process; on real
    # hardware it is generated on-device and never leaves it.
    public_key_hex: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    private_key_hex: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    asset: Mapped[Asset] = relationship(back_populates="devices")


class AnchorBatch(Base):
    __tablename__ = "anchor_batches"
    __table_args__ = (Index("ix_batches_asset_interval", "asset_id", "interval_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    interval_start: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    interval_end: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    merkle_root: Mapped[str] = mapped_column(String(64), nullable=False)
    reading_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trusted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_energy_wh: Mapped[float] = mapped_column(Float, nullable=False)
    rejected_energy_wh: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # "local-anchor" in Stage 2; a real Polygon Amoy transaction hash in Stage 3.
    anchor_reference: Mapped[str] = mapped_column(String(80), nullable=False)
    anchor_target: Mapped[str] = mapped_column(String(40), default="local-anchor")
    anchored_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (
        UniqueConstraint("device_id", "sequence", name="uq_readings_device_sequence"),
        Index("ix_readings_asset_recorded", "asset_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    ac_power_w: Mapped[float] = mapped_column(Float, nullable=False)
    dc_power_w: Mapped[float] = mapped_column(Float, nullable=False)
    poa_irradiance_w_m2: Mapped[float] = mapped_column(Float, nullable=False)
    ghi_w_m2: Mapped[float] = mapped_column(Float, nullable=False)
    module_temp_c: Mapped[float] = mapped_column(Float, nullable=False)
    ambient_temp_c: Mapped[float] = mapped_column(Float, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    energy_wh: Mapped[float] = mapped_column(Float, nullable=False)
    cumulative_energy_wh: Mapped[float] = mapped_column(Float, nullable=False)

    # Verification
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trust_flags: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    injected_attack: Mapped[str | None] = mapped_column(String(60), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("anchor_batches.id"), nullable=True, index=True
    )


class RevenueEvent(Base):
    __tablename__ = "revenue_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("anchor_batches.id"), nullable=True
    )
    period_start: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    verified_energy_wh: Mapped[float] = mapped_column(Float, nullable=False)
    gross_micro_usdc: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_fee_micro_usdc: Mapped[int] = mapped_column(Integer, nullable=False)
    opex_micro_usdc: Mapped[int] = mapped_column(Integer, nullable=False)
    net_micro_usdc: Mapped[int] = mapped_column(Integer, nullable=False)
    distributed_micro_usdc: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ImpactRecord(Base):
    __tablename__ = "impact_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("anchor_batches.id"), nullable=True
    )
    period_start: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    verified_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    rejected_kwh: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    emission_factor_kg_per_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    emission_factor_source: Mapped[str] = mapped_column(String(200), nullable=False)
    co2e_avoided_kg: Mapped[float] = mapped_column(Float, nullable=False)
    certificate_status: Mapped[str] = mapped_column(String(40), nullable=False)
    is_claimable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    method_version: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class TokenLedger(Base):
    __tablename__ = "token_ledgers"

    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    total_supply: Mapped[int] = mapped_column(Integer, nullable=False)
    token_price_micro_usdc: Mapped[int] = mapped_column(Integer, nullable=False)
    # Scaled by 1e18 exactly as the Solidity accumulator will be. A uint256 does
    # not fit a 64-bit column, so it is persisted as text - the same thing any
    # indexer does with on-chain uint256 values.
    acc_per_token: Mapped[str] = mapped_column(String(80), nullable=False, default="0")
    total_deposited_micro_usdc: Mapped[int] = mapped_column(Integer, default=0)
    total_claimed_micro_usdc: Mapped[int] = mapped_column(Integer, default=0)
    undistributed_micro_usdc: Mapped[int] = mapped_column(Integer, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str] = mapped_column(String(200), default="")


class TokenHolder(Base):
    __tablename__ = "token_holders"
    __table_args__ = (
        UniqueConstraint("asset_id", "address", name="uq_holder_asset_address"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    address: Mapped[str] = mapped_column(String(80), nullable=False)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    reward_debt: Mapped[int] = mapped_column(Integer, default=0)
    accrued_micro_usdc: Mapped[int] = mapped_column(Integer, default=0)
    claimed_micro_usdc: Mapped[int] = mapped_column(Integer, default=0)
    # Mock KYC: a real vendor is a paid service, so Stage 2 uses a local registry
    # with the same gate semantics the Solidity transfer hook will enforce.
    kyc_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AttackEvent(Base):
    __tablename__ = "attack_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attack_type: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    detected: Mapped[bool] = mapped_column(Boolean, default=False)
    detected_by: Mapped[str] = mapped_column(String(200), default="")
    trust_score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
