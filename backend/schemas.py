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


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str
    environment: str
    server_time: datetime
    simulator_enabled: bool
    reading_count: int


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
    device_count: int = 0


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    payload_hash: str | None = None


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

    current_power_w: float = Field(default=0.0, description="Latest AC power output")
    current_power_percent_of_ac_capacity: float = 0.0
    ac_capacity_kw: float
    dc_capacity_kw: float

    energy_today_kwh: float = 0.0
    energy_total_kwh: float = Field(
        default=0.0, description="Cumulative energy since monitoring began"
    )
    peak_power_today_w: float = 0.0
    capacity_factor_today_percent: float = 0.0
    specific_yield_today_kwh_per_kwp: float = 0.0
    co2_avoided_total_kg: float = 0.0

    module_temp_c: float | None = None
    ambient_temp_c: float | None = None
    poa_irradiance_w_m2: float | None = None
    device_count: int = 0
    reading_count: int = 0
