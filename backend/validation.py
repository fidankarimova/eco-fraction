"""Validation engine - the layer that decides whether a reading may be trusted.

Nothing reaches an anchored batch, an impact record or a payout unless it passes
here. That ordering is the project's actual thesis: trust is a *precondition of
payment*, not a badge on a dashboard.

Eight independent checks, deliberately of three different kinds so that no single
compromise defeats them all:

* **Cryptographic** - signature, sequence monotonicity, timestamp window.
  Catches edited, replayed and back-dated readings.
* **Physical** - clear-sky ceiling, night generation, rate of change.
  Catches values that no real array could produce, *even when correctly signed by
  a compromised device*.
* **Cross-reference** - declared irradiance against an independent model of the
  same instant, under the same weather. Catches a device reporting
  internally-consistent fiction, including a quiet constant multiplier that every
  single-reading check would pass.

The physical and cross-reference checks are the ones that matter for the oracle
problem: they do not rely on the device being honest, only on physics.

Detection statistics are measured, not asserted - see ``tests/test_verification.py``
and ``scripts/evidence_report.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from backend.crypto_util import verify_measurement
from backend.simulation.solar import (
    clearness_index,
    haurwitz_clear_sky_ghi,
    plane_of_array_irradiance,
    solar_position,
)

# Weight per check. Signature failure alone is disqualifying; the physical checks
# are weighted so that any two failures also drop a reading below the threshold.
CHECK_WEIGHTS: dict[str, float] = {
    "signature": 0.28,
    "sequence": 0.14,
    "timestamp": 0.09,
    "night_generation": 0.14,
    "clear_sky_ceiling": 0.14,
    "rate_of_change": 0.05,
    "irradiance_consistency": 0.11,
    "sustained_bias": 0.05,
}

# A single reading cannot reveal a small calibration fraud - an 18% over-report on
# a cloudy afternoon is indistinguishable from a sunnier minute. A *persistent*
# positive bias against the independent reference can be detected, and that is the
# realistic fraud: not one absurd spike, but a quiet constant multiplier.
BIAS_WINDOW_MIN_SAMPLES = 8
BIAS_RATIO_LIMIT = 1.08

# A reading is trusted only if it passes EVERY check. The weighted score is a
# graded quality indicator for the dashboard, not the gate: accepting a reading
# that failed a physical-plausibility check because it passed the cryptographic
# ones would defeat the purpose of having both kinds.
TRUST_THRESHOLD = 1.0
MAX_CLOCK_SKEW_SECONDS = 120
MAX_BACKDATE_SECONDS = 3600
# A real array cannot exceed clear-sky plane-of-array irradiance; allow headroom
# for cloud-edge enhancement, which is a genuine effect.
CLEAR_SKY_TOLERANCE = 1.35

# The cross-reference checks compare against an observer of the *same weather*,
# not against clear sky, so their tolerance is a margin for model disagreement
# between two observers - not a margin for the entire cloud cover of the day.
REFERENCE_TOLERANCE = 1.35
# Full swing of the inverter rating in one second is not physically possible.
MAX_RAMP_W_PER_SECOND_FRACTION = 0.5


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    observed: float | None = None
    expected: float | None = None


@dataclass
class ValidationOutcome:
    trust_score: float
    is_trusted: bool
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failed_checks(self) -> list[str]:
        return [check.name for check in self.checks if not check.passed]

    @property
    def flags(self) -> str:
        return ",".join(self.failed_checks)

    def as_dict(self) -> dict:
        return {
            "trust_score": self.trust_score,
            "is_trusted": self.is_trusted,
            "failed_checks": self.failed_checks,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "observed": c.observed,
                    "expected": c.expected,
                }
                for c in self.checks
            ],
        }


@dataclass
class DeviceContext:
    """Everything the validator needs that is not in the reading itself."""

    public_key_hex: str
    last_sequence: int | None = None
    last_recorded_at: datetime | None = None
    last_ac_power_w: float | None = None
    # Ratio of declared to independently modelled irradiance for recent readings.
    recent_bias_ratios: list[float] = field(default_factory=list)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def clear_sky_poa(asset, when: datetime) -> tuple[float, float]:
    """Clear-sky irradiance on the asset's tilted plane, and the sun's elevation.

    The reference has to be transposed onto the same plane as the measurement. A
    30-degree tilted panel legitimately receives *more* than horizontal irradiance
    at low sun angles, so comparing a declared plane-of-array value against
    horizontal GHI would flag honest readings as fraudulent - a false-positive
    generator, and the last thing this system can afford.
    """
    when = _as_utc(when)
    elevation, azimuth = solar_position(asset.latitude, asset.longitude, when)
    ghi = haurwitz_clear_sky_ghi(elevation)
    poa = plane_of_array_irradiance(
        ghi=ghi,
        elevation_deg=elevation,
        azimuth_deg=azimuth,
        day_of_year=when.timetuple().tm_yday,
        tilt_deg=asset.tilt_deg,
        surface_azimuth_deg=asset.azimuth_deg,
        albedo=asset.albedo,
    )
    return poa, elevation


def observed_reference_poa(asset, when: datetime) -> tuple[float, float]:
    """Plane-of-array irradiance an independent observer of the same sky would report.

    The *ceiling* checks compare against clear sky, because no array can out-produce
    clear sky whatever the weather. The cross-reference checks cannot use that same
    number: under cloud an honest device legitimately reports a fraction of the
    clear-sky value, and that slack is exactly where a quiet multiplier hides. An
    18% over-report on a 70%-clouded afternoon still lands far below the clear-sky
    line, so a clear-sky reference can never see it - no matter how many readings
    it averages.

    So this reference models the cloud cover the sky actually had, the way a
    neighbouring station or a satellite pass would observe it. The weather model is
    deterministic and keyed on the asset's published weather seed, which is what
    lets a reviewer recompute this reference for any past instant and get the same
    number the validator used.
    """
    when = _as_utc(when)
    elevation, azimuth = solar_position(asset.latitude, asset.longitude, when)
    ghi = haurwitz_clear_sky_ghi(elevation) * clearness_index(when, asset.weather_seed)
    poa = plane_of_array_irradiance(
        ghi=ghi,
        elevation_deg=elevation,
        azimuth_deg=azimuth,
        day_of_year=when.timetuple().tm_yday,
        tilt_deg=asset.tilt_deg,
        surface_azimuth_deg=asset.azimuth_deg,
        albedo=asset.albedo,
    )
    return poa, elevation


def validate_measurement(
    measurement: dict,
    signature_hex: str | None,
    context: DeviceContext,
    asset,
    now: datetime | None = None,
) -> ValidationOutcome:
    """Run every check and return a weighted trust score."""
    now = _as_utc(now or datetime.now(tz=timezone.utc))
    recorded_at = _as_utc(measurement["recorded_at"])
    checks: list[CheckResult] = []

    # --- cryptographic -----------------------------------------------------
    signature_ok = bool(signature_hex) and verify_measurement(
        context.public_key_hex, measurement, signature_hex
    )
    checks.append(
        CheckResult(
            "signature",
            signature_ok,
            "Ed25519 signature verifies against the registered device key"
            if signature_ok
            else "Signature missing or does not match the registered device key",
        )
    )

    sequence = int(measurement["sequence"])
    sequence_ok = context.last_sequence is None or sequence > context.last_sequence
    checks.append(
        CheckResult(
            "sequence",
            sequence_ok,
            "Sequence advances monotonically"
            if sequence_ok
            else f"Sequence {sequence} does not advance past {context.last_sequence} (replay)",
            observed=float(sequence),
            expected=float(context.last_sequence + 1) if context.last_sequence else None,
        )
    )

    drift = (recorded_at - now).total_seconds()
    timestamp_ok = -MAX_BACKDATE_SECONDS <= drift <= MAX_CLOCK_SKEW_SECONDS
    checks.append(
        CheckResult(
            "timestamp",
            timestamp_ok,
            "Timestamp within the accepted window"
            if timestamp_ok
            else f"Timestamp is {drift:.0f}s from server time",
            observed=round(drift, 1),
        )
    )

    # --- physical ----------------------------------------------------------
    reference_poa, elevation = clear_sky_poa(asset, recorded_at)
    ac_power_w = float(measurement["ac_power_w"])
    poa = float(measurement["poa_irradiance_w_m2"])

    night_ok = elevation > 0.0 or ac_power_w <= 1.0
    checks.append(
        CheckResult(
            "night_generation",
            night_ok,
            "No output claimed while the sun is below the horizon"
            if night_ok
            else f"{ac_power_w:.0f} W claimed with the sun {elevation:.1f}deg below horizon",
            observed=ac_power_w,
            expected=0.0,
        )
    )

    # A real array cannot out-produce clear-sky conditions on its own plane.
    max_poa = reference_poa * CLEAR_SKY_TOLERANCE + 20.0
    max_power_w = (
        asset.dc_capacity_kw * 1000.0 * (max_poa / 1000.0) * asset.inverter_efficiency
    )
    max_power_w = min(max_power_w, asset.ac_capacity_kw * 1000.0)
    ceiling_ok = ac_power_w <= max_power_w + 1.0
    checks.append(
        CheckResult(
            "clear_sky_ceiling",
            ceiling_ok,
            "Output within the clear-sky physical ceiling"
            if ceiling_ok
            else f"{ac_power_w:.0f} W exceeds the {max_power_w:.0f} W physical ceiling",
            observed=ac_power_w,
            expected=round(max_power_w, 1),
        )
    )

    ramp_ok = True
    ramp_detail = "Rate of change is physically plausible"
    if context.last_ac_power_w is not None and context.last_recorded_at is not None:
        gap = max((recorded_at - _as_utc(context.last_recorded_at)).total_seconds(), 1.0)
        ramp = abs(ac_power_w - context.last_ac_power_w) / gap
        limit = asset.ac_capacity_kw * 1000.0 * MAX_RAMP_W_PER_SECOND_FRACTION
        ramp_ok = ramp <= limit
        if not ramp_ok:
            ramp_detail = f"Output changed {ramp:.0f} W/s, limit {limit:.0f} W/s"
    checks.append(CheckResult("rate_of_change", ramp_ok, ramp_detail))

    # --- cross-reference ---------------------------------------------------
    # The device declares its own irradiance. Compare it with an independent model
    # of the same instant - one that saw the same clouds, not an idealised clear
    # sky. In production this reference is satellite irradiance or a neighbouring
    # station; here it is the deterministic weather model, recomputable by anyone.
    observed_poa, _ = observed_reference_poa(asset, recorded_at)
    reference_ceiling = observed_poa * REFERENCE_TOLERANCE + 20.0
    irradiance_ok = poa <= reference_ceiling
    checks.append(
        CheckResult(
            "irradiance_consistency",
            irradiance_ok,
            "Declared irradiance is consistent with the independent reference"
            if irradiance_ok
            else f"Declared {poa:.0f} W/m2 exceeds the {reference_ceiling:.0f} W/m2 reference ceiling",
            observed=poa,
            expected=round(reference_ceiling, 1),
        )
    )

    bias_ok = True
    bias_detail = "No sustained deviation from the independent reference"
    ratios = list(context.recent_bias_ratios)
    if observed_poa > 50.0:
        ratios.append(poa / observed_poa)
    if len(ratios) >= BIAS_WINDOW_MIN_SAMPLES:
        window = ratios[-BIAS_WINDOW_MIN_SAMPLES:]
        mean_ratio = sum(window) / len(window)
        bias_ok = mean_ratio <= BIAS_RATIO_LIMIT
        if not bias_ok:
            bias_detail = (
                f"Declared irradiance averages {mean_ratio:.2f}x the independent "
                f"reference over {len(window)} readings - sustained over-reporting"
            )
    checks.append(CheckResult("sustained_bias", bias_ok, bias_detail))

    score = sum(CHECK_WEIGHTS[check.name] for check in checks if check.passed)
    score = round(min(1.0, max(0.0, score)), 4)
    return ValidationOutcome(
        trust_score=score,
        is_trusted=all(check.passed for check in checks),
        checks=checks,
    )


def asset_trust_summary(readings: list) -> dict:
    """Aggregate trust over a window of already-validated readings."""
    if not readings:
        return {
            "sample_count": 0,
            "trusted_count": 0,
            "rejected_count": 0,
            "trust_rate_percent": 100.0,
            "average_trust_score": 1.0,
            "failing_checks": {},
        }

    trusted = [r for r in readings if r.is_trusted]
    failing: dict[str, int] = {}
    for reading in readings:
        for flag in (reading.trust_flags or "").split(","):
            if flag:
                failing[flag] = failing.get(flag, 0) + 1

    return {
        "sample_count": len(readings),
        "trusted_count": len(trusted),
        "rejected_count": len(readings) - len(trusted),
        "trust_rate_percent": round(len(trusted) / len(readings) * 100.0, 2),
        "average_trust_score": round(
            sum(r.trust_score for r in readings) / len(readings), 4
        ),
        "failing_checks": dict(sorted(failing.items(), key=lambda kv: -kv[1])),
    }
