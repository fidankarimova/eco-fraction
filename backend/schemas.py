"""API response models.

These are the public contract. The ORM models can change shape without breaking
the dashboard as long as these stay stable.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class SystemStatus(str, Enum):
    GENERATING = "generating"
    STANDBY = "standby"
    NIGHT = "night"
    OFFLINE = "offline"
    NO_DATA = "no_data"
    UNTRUSTED = "untrusted"


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str
    environment: str
    server_time: datetime
    simulator_enabled: bool
    reading_count: int
    schema_version: int


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    location_name: str
    latitude: float
    longitude: float
    utc_offset_hours: float
    dc_capacity_kw: float
    ac_capacity_kw: float
    tilt_deg: float
    azimuth_deg: float
    grid_emission_factor_kg_per_kwh: float
    emission_factor_source: str
    certificate_status: str
    device_count: int = 0


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recorded_at: datetime
    device_id: str
    sequence: int
    ac_power_w: float
    dc_power_w: float
    poa_irradiance_w_m2: float
    ghi_w_m2: float
    module_temp_c: float
    ambient_temp_c: float
    energy_wh: float
    cumulative_energy_wh: float
    payload_hash: str
    signature: str | None = None
    trust_score: float
    is_trusted: bool
    trust_flags: str = ""
    injected_attack: str | None = None
    batch_id: int | None = None


class ReadingPage(BaseModel):
    asset_id: str
    count: int
    start: datetime | None = None
    end: datetime | None = None
    readings: list[ReadingOut]


class SeriesPoint(BaseModel):
    bucket_start: datetime
    average_power_w: float
    peak_power_w: float
    energy_wh: float
    average_poa_w_m2: float
    sample_count: int
    rejected_count: int = 0


class SeriesResponse(BaseModel):
    asset_id: str
    window_hours: float
    bucket_minutes: float
    point_count: int
    total_energy_kwh: float
    points: list[SeriesPoint]


class AssetSummary(BaseModel):
    asset_id: str
    asset_name: str
    location_name: str
    status: SystemStatus
    status_detail: str
    server_time: datetime
    last_reading_at: datetime | None = None
    data_age_seconds: float | None = None

    current_power_w: float = Field(default=0.0, description="Latest verified AC power")
    current_power_percent_of_ac_capacity: float = 0.0
    ac_capacity_kw: float
    dc_capacity_kw: float

    energy_today_kwh: float = 0.0
    energy_total_kwh: float = Field(
        default=0.0, description="Cumulative verified energy since monitoring began"
    )
    peak_power_today_w: float = 0.0
    capacity_factor_today_percent: float = 0.0
    specific_yield_today_kwh_per_kwp: float = 0.0

    co2_avoided_total_kg: float = 0.0
    co2_is_claimable: bool = False
    certificate_status: str = "unknown"
    emission_factor_source: str = ""

    module_temp_c: float | None = None
    ambient_temp_c: float | None = None
    poa_irradiance_w_m2: float | None = None
    device_count: int = 0
    reading_count: int = 0
    rejected_reading_count: int = 0
    latest_trust_score: float = 1.0
    latest_failed_checks: list[str] = Field(default_factory=list)


# --- verification ----------------------------------------------------------


class AttackDefinitionOut(BaseModel):
    key: str
    title: str
    story: str
    expected_detection: str
    attacker_holds_device_key: bool


class AttackResult(BaseModel):
    attack: str
    title: str
    story: str
    detected: bool
    trust_score: float
    failed_checks: list[str]
    reported_power_w: float
    checks: list[dict]
    explanation: str


class TrustReport(BaseModel):
    asset_id: str
    window_hours: float
    sample_count: int
    trusted_count: int
    rejected_count: int
    trust_rate_percent: float
    average_trust_score: float
    failing_checks: dict[str, int]
    anchored_batch_count: int
    latest_batch: dict | None = None
    attack_total: int = 0
    attack_detected: int = 0
    attack_detection_rate_percent: float | None = None
    recent_attacks: list[dict] = Field(default_factory=list)


# --- tokenisation ----------------------------------------------------------


class PurchaseRequest(BaseModel):
    address: str = Field(min_length=3, max_length=80)
    token_count: int = Field(ge=1, le=2_500)


class ClaimRequest(BaseModel):
    address: str = Field(min_length=3, max_length=80)


class HolderView(BaseModel):
    address: str
    token_balance: int
    ownership_percent: float
    investment_usdc: float
    claimable_usdc: float
    claimed_usdc: float


class LedgerSummary(BaseModel):
    asset_id: str
    total_supply: int
    circulating_supply: int
    unsold_supply: int
    token_price_usdc: float
    holder_count: int
    total_distributed_usdc: float
    total_claimed_usdc: float
    undistributed_usdc: float
    paused: bool
    pause_reason: str
    minimum_investment_usdc: float
    settlement_asset: str = "mock-USDC (test token, no real value)"


class ClaimResult(BaseModel):
    address: str
    claimed_usdc: float
    settlement_asset: str = "mock-USDC (test token, no real value)"
    note: str
    holder: HolderView
