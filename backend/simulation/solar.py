"""Physically-grounded solar generation model.

The point of this module is that the generated data is *not* a sine wave with
noise on top. It is computed the way a PV performance model computes it:

1. solar position from the NOAA solar-position equations
2. clear-sky global horizontal irradiance from the Haurwitz model
3. a diffuse/direct split using the Erbs correlation
4. transposition onto the tilted module plane (isotropic sky + ground albedo)
5. module temperature from the NOCT model
6. DC power with a temperature coefficient, then inverter efficiency and AC clipping

Cloud cover is layered on as *deterministic* value noise keyed on the asset's
weather seed and the timestamp. Determinism matters: the historical backfill and
the live tick use the same function, so the curve is continuous across a restart
and a reviewer can reproduce any past reading exactly.

Standard library only - no numpy, no paid services, no network calls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

SOLAR_CONSTANT_W_M2 = 1367.0
STC_IRRADIANCE_W_M2 = 1000.0
NOCT_REFERENCE_IRRADIANCE = 800.0
STC_CELL_TEMP_C = 25.0


@dataclass(frozen=True)
class SiteConfig:
    """Everything the model needs about one generator."""

    latitude: float
    longitude: float
    dc_capacity_kw: float
    ac_capacity_kw: float
    tilt_deg: float = 30.0
    azimuth_deg: float = 180.0  # 180 = facing the equator in the northern hemisphere
    temp_coefficient_per_c: float = -0.0035
    noct_c: float = 45.0
    albedo: float = 0.20
    soiling_loss: float = 0.02
    inverter_efficiency: float = 0.97
    weather_seed: int = 1


@dataclass(frozen=True)
class SolarSample:
    """One instantaneous model evaluation."""

    timestamp: datetime
    solar_elevation_deg: float
    solar_azimuth_deg: float
    clear_sky_ghi_w_m2: float
    ghi_w_m2: float
    poa_irradiance_w_m2: float
    ambient_temp_c: float
    module_temp_c: float
    dc_power_w: float
    ac_power_w: float
    clearness_index: float

    @property
    def is_daylight(self) -> bool:
        return self.solar_elevation_deg > 0.0


# ---------------------------------------------------------------------------
# Deterministic smooth noise
# ---------------------------------------------------------------------------


def _hash01(n: int, seed: int) -> float:
    """Map an integer lattice point to a stable pseudo-random value in [0, 1)."""
    x = (n * 0x9E3779B1 + seed * 0x85EBCA77) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x2C1B3C6D) & 0xFFFFFFFF
    x ^= x >> 12
    x = (x * 0x297A2D39) & 0xFFFFFFFF
    x ^= x >> 15
    return x / 0xFFFFFFFF


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _value_noise(x: float, seed: int) -> float:
    """Smooth 1-D noise in [0, 1) with continuous first differences."""
    i = math.floor(x)
    frac = x - i
    a = _hash01(int(i), seed)
    b = _hash01(int(i) + 1, seed)
    return a + (b - a) * _smoothstep(frac)


def _daily_regime(day_index: int, seed: int) -> float:
    """Mean clearness for the whole day: overcast, mixed or clear."""
    roll = _hash01(day_index, seed * 7919)
    if roll < 0.15:
        return 0.32  # overcast
    if roll < 0.50:
        return 0.68  # broken cloud
    return 0.94  # clear


def clearness_index(when: datetime, seed: int) -> float:
    """Fraction of clear-sky irradiance actually reaching the ground."""
    epoch = when.timestamp()
    day_index = int(epoch // 86_400)
    base = _daily_regime(day_index, seed)

    # Three octaves: slow fronts, cumulus passing, fast edge flicker.
    slow = _value_noise(epoch / 5400.0, seed + 11)
    medium = _value_noise(epoch / 900.0, seed + 23)
    fast = _value_noise(epoch / 120.0, seed + 41)

    variability = 1.0 - base  # clear days fluctuate less
    modulation = (
        0.60 * (slow - 0.5) + 0.30 * (medium - 0.5) + 0.10 * (fast - 0.5)
    ) * 2.0
    value = base + variability * modulation * 0.9
    return min(1.0, max(0.05, value))


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------


def solar_position(latitude: float, longitude: float, when: datetime) -> tuple[float, float]:
    """Return (elevation_deg, azimuth_deg) using the NOAA solar equations."""
    when = when.astimezone(timezone.utc)
    day_of_year = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0

    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour - 12.0) / 24.0)

    eq_time_min = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    true_solar_time = hour * 60.0 + eq_time_min + 4.0 * longitude
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    cos_zenith = min(1.0, max(-1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = math.degrees(math.pi / 2.0 - zenith)

    sin_zenith = math.sin(zenith)
    if sin_zenith < 1e-6:
        azimuth = 180.0
    else:
        cos_az = (
            math.sin(declination) * math.cos(lat)
            - math.cos(declination) * math.sin(lat) * math.cos(hour_angle)
        ) / sin_zenith
        cos_az = min(1.0, max(-1.0, cos_az))
        azimuth = math.degrees(math.acos(cos_az))
        if math.sin(hour_angle) > 0:
            azimuth = 360.0 - azimuth

    return elevation, azimuth


def extraterrestrial_irradiance(day_of_year: int) -> float:
    """Top-of-atmosphere normal irradiance, corrected for orbital eccentricity."""
    return SOLAR_CONSTANT_W_M2 * (
        1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)
    )


def haurwitz_clear_sky_ghi(elevation_deg: float) -> float:
    """Clear-sky GHI (W/m^2). Haurwitz 1945 - one parameter, well behaved."""
    if elevation_deg <= 0.0:
        return 0.0
    cos_zenith = math.sin(math.radians(elevation_deg))
    if cos_zenith <= 0.0:
        return 0.0
    return max(0.0, 1098.0 * cos_zenith * math.exp(-0.059 / cos_zenith))


def erbs_diffuse_fraction(kt: float) -> float:
    """Diffuse fraction of GHI from the clearness index (Erbs et al. 1982)."""
    if kt <= 0.22:
        return 1.0 - 0.09 * kt
    if kt <= 0.80:
        return (
            0.9511
            - 0.1604 * kt
            + 4.388 * kt**2
            - 16.638 * kt**3
            + 12.336 * kt**4
        )
    return 0.165


def plane_of_array_irradiance(
    ghi: float,
    elevation_deg: float,
    azimuth_deg: float,
    day_of_year: int,
    tilt_deg: float,
    surface_azimuth_deg: float,
    albedo: float,
) -> float:
    """Transpose GHI onto the tilted module plane (isotropic sky model)."""
    if ghi <= 0.0 or elevation_deg <= 0.0:
        return 0.0

    zenith = math.radians(90.0 - elevation_deg)
    cos_zenith = max(math.cos(zenith), 0.0)
    if cos_zenith <= 0.0:
        return 0.0

    e0 = extraterrestrial_irradiance(day_of_year)
    kt = min(1.0, max(0.0, ghi / max(e0 * cos_zenith, 1e-6)))
    diffuse_fraction = erbs_diffuse_fraction(kt)

    dhi = ghi * diffuse_fraction
    dni = (ghi - dhi) / max(cos_zenith, 1e-6)

    tilt = math.radians(tilt_deg)
    delta_azimuth = math.radians(azimuth_deg - surface_azimuth_deg)
    cos_aoi = cos_zenith * math.cos(tilt) + math.sin(zenith) * math.sin(tilt) * math.cos(
        delta_azimuth
    )
    cos_aoi = max(cos_aoi, 0.0)

    beam = dni * cos_aoi
    sky_diffuse = dhi * (1.0 + math.cos(tilt)) / 2.0
    ground_reflected = ghi * albedo * (1.0 - math.cos(tilt)) / 2.0
    return max(0.0, beam + sky_diffuse + ground_reflected)


def ambient_temperature_c(when: datetime, utc_offset_hours: float, seed: int) -> float:
    """Diurnal + seasonal ambient temperature, deliberately simple but plausible."""
    when = when.astimezone(timezone.utc)
    day_of_year = when.timetuple().tm_yday
    local_hour = (
        when.hour + when.minute / 60.0 + when.second / 3600.0 + utc_offset_hours
    ) % 24.0

    seasonal_mean = 14.5 - 11.0 * math.cos(2.0 * math.pi * (day_of_year - 20) / 365.0)
    diurnal_swing = 7.0 * math.sin(2.0 * math.pi * (local_hour - 9.0) / 24.0)
    weather_drift = (_value_noise(when.timestamp() / 21_600.0, seed + 97) - 0.5) * 4.0
    return seasonal_mean + diurnal_swing + weather_drift


# ---------------------------------------------------------------------------
# Full model evaluation
# ---------------------------------------------------------------------------


def evaluate(site: SiteConfig, when: datetime, utc_offset_hours: float = 0.0) -> SolarSample:
    """Evaluate the whole chain at one instant."""
    when = when.astimezone(timezone.utc)
    day_of_year = when.timetuple().tm_yday

    elevation, azimuth = solar_position(site.latitude, site.longitude, when)
    clear_sky = haurwitz_clear_sky_ghi(elevation)
    clearness = clearness_index(when, site.weather_seed)
    ghi = clear_sky * clearness

    poa = plane_of_array_irradiance(
        ghi=ghi,
        elevation_deg=elevation,
        azimuth_deg=azimuth,
        day_of_year=day_of_year,
        tilt_deg=site.tilt_deg,
        surface_azimuth_deg=site.azimuth_deg,
        albedo=site.albedo,
    )

    ambient = ambient_temperature_c(when, utc_offset_hours, site.weather_seed)
    module_temp = ambient + (poa / NOCT_REFERENCE_IRRADIANCE) * (site.noct_c - 20.0)

    temp_factor = 1.0 + site.temp_coefficient_per_c * (module_temp - STC_CELL_TEMP_C)
    dc_power_w = (
        site.dc_capacity_kw
        * 1000.0
        * (poa / STC_IRRADIANCE_W_M2)
        * max(temp_factor, 0.0)
        * (1.0 - site.soiling_loss)
    )
    dc_power_w = max(0.0, dc_power_w)

    ac_power_w = min(
        dc_power_w * site.inverter_efficiency, site.ac_capacity_kw * 1000.0
    )
    if dc_power_w < site.dc_capacity_kw * 1000.0 * 0.001:
        # Inverters do not export below their start-up threshold.
        ac_power_w = 0.0

    return SolarSample(
        timestamp=when,
        solar_elevation_deg=round(elevation, 3),
        solar_azimuth_deg=round(azimuth, 3),
        clear_sky_ghi_w_m2=round(clear_sky, 2),
        ghi_w_m2=round(ghi, 2),
        poa_irradiance_w_m2=round(poa, 2),
        ambient_temp_c=round(ambient, 2),
        module_temp_c=round(module_temp, 2),
        dc_power_w=round(dc_power_w, 2),
        ac_power_w=round(ac_power_w, 2),
        clearness_index=round(clearness, 4),
    )
