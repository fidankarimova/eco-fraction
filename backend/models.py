"""Persistence models.

Three tables in Stage 1:

* ``assets``   - the physical generator being monitored (and later tokenised)
* ``devices``  - the metering units attached to an asset
* ``readings`` - the immutable measurement stream

The reading row already carries ``sequence`` and ``payload_hash`` because Stage 2
adds per-device signatures and Merkle batching on top of exactly these fields.
Nothing in Stage 1 depends on them, but writing them now means no migration later.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    firmware: Mapped[str] = mapped_column(String(40), default="0.1.0")
    sample_interval_seconds: Mapped[int] = mapped_column(Integer, default=5)
    is_simulated: Mapped[bool] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="devices")


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
    recorded_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, index=True
    )
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

    # Reserved for Stage 2 (device signatures + Merkle anchoring).
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
