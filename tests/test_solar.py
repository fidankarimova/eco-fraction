"""Tests for the solar model.

These assert physical behaviour rather than exact numbers: no output at night,
a plausible daily yield, monotonic energy, and bounded irradiance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.simulation.solar import (
    SiteConfig,
    clearness_index,
    erbs_diffuse_fraction,
    evaluate,
    haurwitz_clear_sky_ghi,
    solar_position,
)

BAKU = SiteConfig(
    latitude=40.4093,
    longitude=49.8671,
    dc_capacity_kw=12.5,
    ac_capacity_kw=10.0,
    weather_seed=20260810,
)
UTC_OFFSET = 4.0


def _local(year, month, day, hour) -> datetime:
    """A plant-local wall-clock time expressed as UTC."""
    return datetime(year, month, day, hour, tzinfo=timezone.utc) - timedelta(
        hours=UTC_OFFSET
    )


def test_no_generation_at_local_midnight():
    sample = evaluate(BAKU, _local(2026, 8, 10, 0), UTC_OFFSET)
    assert sample.solar_elevation_deg < 0
    assert sample.ghi_w_m2 == 0.0
    assert sample.ac_power_w == 0.0
    assert sample.is_daylight is False


def test_generation_around_local_noon():
    sample = evaluate(BAKU, _local(2026, 8, 10, 13), UTC_OFFSET)
    assert sample.solar_elevation_deg > 50
    assert sample.poa_irradiance_w_m2 > 300
    assert sample.ac_power_w > 3000


def test_ac_power_never_exceeds_inverter_rating():
    start = _local(2026, 6, 21, 0)
    for minutes in range(0, 24 * 60, 10):
        sample = evaluate(BAKU, start + timedelta(minutes=minutes), UTC_OFFSET)
        assert 0.0 <= sample.ac_power_w <= BAKU.ac_capacity_kw * 1000 + 1e-6


def test_summer_peak_elevation_matches_latitude():
    """At the solstice, peak elevation ~= 90 - latitude + 23.44 degrees."""
    best = max(
        solar_position(BAKU.latitude, BAKU.longitude, _local(2026, 6, 21, 0)
                       + timedelta(minutes=m))[0]
        for m in range(0, 24 * 60, 5)
    )
    expected = 90.0 - BAKU.latitude + 23.44
    assert best == pytest.approx(expected, abs=1.5)


def test_daily_yield_is_physically_plausible():
    """A summer day in Baku should land in the 3-8 kWh/kWp band."""
    start = _local(2026, 8, 10, 0)
    step_minutes = 10
    energy_wh = sum(
        evaluate(BAKU, start + timedelta(minutes=m), UTC_OFFSET).ac_power_w
        * (step_minutes / 60.0)
        for m in range(0, 24 * 60, step_minutes)
    )
    specific_yield = energy_wh / 1000.0 / BAKU.dc_capacity_kw
    assert 3.0 < specific_yield < 8.0


def test_clear_sky_model_bounds():
    assert haurwitz_clear_sky_ghi(-5) == 0.0
    assert haurwitz_clear_sky_ghi(0) == 0.0
    assert 900 < haurwitz_clear_sky_ghi(90) < 1100


def test_clearness_index_in_range_and_deterministic():
    when = datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
    first = clearness_index(when, 7)
    second = clearness_index(when, 7)
    assert first == second
    assert 0.05 <= first <= 1.0
    assert clearness_index(when, 8) != first


def test_clearness_index_is_smooth():
    """Consecutive seconds must not jump: the curve has to look like weather."""
    base = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    previous = clearness_index(base, 3)
    for second in range(1, 120):
        current = clearness_index(base + timedelta(seconds=second), 3)
        assert abs(current - previous) < 0.02
        previous = current


def test_erbs_diffuse_fraction_monotone_regions():
    assert erbs_diffuse_fraction(0.0) == pytest.approx(1.0, abs=0.01)
    assert erbs_diffuse_fraction(0.9) == pytest.approx(0.165)
    assert 0.0 < erbs_diffuse_fraction(0.5) < 1.0
