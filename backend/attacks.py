"""Adversarial attack injection.

The hardest jury question this project faces is: *if the plant owner controls the
sensor, what stops them from lying?* Arguing the answer is weak. Letting someone
run the attack and watch it get caught is not.

Each attack below is a realistic manipulation of the telemetry path. Injection
happens **after** the honest measurement is produced and **before** validation, so
the validator sees exactly what a compromised device or a man-in-the-middle would
send. The validator has no knowledge that an attack is in progress.

Two of these attacks defeat the signature and are caught only by physics
(``inflate_output``, ``phantom_night``, ``scaling_drift``), and one defeats
physics and is caught only by cryptography (``replay``). That is the point of
having both: neither layer alone is sufficient.

The same definitions drive ``tests/test_verification.py``, so the demo and the test
suite cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from backend.crypto_util import sign_measurement


@dataclass(frozen=True)
class AttackDefinition:
    key: str
    title: str
    story: str
    expected_detection: str
    resign: bool  # True = attacker controls the device key and re-signs


def _inflate(measurement: dict, asset) -> dict:
    """Owner reports 3x the real output to inflate revenue and ESG credit."""
    tampered = dict(measurement)
    tampered["ac_power_w"] = round(asset.ac_capacity_kw * 1000.0 * 2.4, 2)
    tampered["dc_power_w"] = round(tampered["ac_power_w"] / 0.97, 2)
    tampered["energy_wh"] = round(
        tampered["ac_power_w"] * measurement["interval_seconds"] / 3600.0, 4
    )
    return tampered


def _phantom_night(measurement: dict, asset) -> dict:
    """Meter claims generation while the sun is below the horizon."""
    tampered = dict(measurement)
    tampered["recorded_at"] = measurement["recorded_at"].replace(
        hour=(measurement["recorded_at"].hour + 12) % 24
    )
    tampered["ac_power_w"] = round(asset.ac_capacity_kw * 1000.0 * 0.55, 2)
    tampered["dc_power_w"] = round(tampered["ac_power_w"] / 0.97, 2)
    tampered["poa_irradiance_w_m2"] = 620.0
    return tampered


def _scaling_drift(measurement: dict, asset) -> dict:
    """Subtle 18% over-reporting - the realistic fraud, not the obvious one."""
    tampered = dict(measurement)
    tampered["ac_power_w"] = round(measurement["ac_power_w"] * 1.18, 2)
    tampered["poa_irradiance_w_m2"] = round(
        measurement["poa_irradiance_w_m2"] * 1.18, 2
    )
    tampered["energy_wh"] = round(
        tampered["ac_power_w"] * measurement["interval_seconds"] / 3600.0, 4
    )
    return tampered


def _replay(measurement: dict, asset) -> dict:
    """Man-in-the-middle resends an earlier high-output reading verbatim."""
    tampered = dict(measurement)
    tampered["sequence"] = max(1, measurement["sequence"] - 5)
    return tampered


def _backdate(measurement: dict, asset) -> dict:
    """Reading back-dated to move energy into a more favourable settlement period."""
    tampered = dict(measurement)
    tampered["recorded_at"] = measurement["recorded_at"] - timedelta(hours=3)
    return tampered


def _tamper_in_transit(measurement: dict, asset) -> dict:
    """Value edited between device and server; the attacker has no device key."""
    tampered = dict(measurement)
    tampered["ac_power_w"] = round(measurement["ac_power_w"] * 1.5 + 500.0, 2)
    tampered["energy_wh"] = round(
        tampered["ac_power_w"] * measurement["interval_seconds"] / 3600.0, 4
    )
    return tampered


ATTACKS: dict[str, tuple[AttackDefinition, Callable[[dict, object], dict]]] = {
    "inflate_output": (
        AttackDefinition(
            key="inflate_output",
            title="Inflated output",
            story="The operator reprograms the meter to report 2.4x the real power, "
            "increasing both revenue and claimed CO2 savings.",
            expected_detection="clear_sky_ceiling",
            resign=True,
        ),
        _inflate,
    ),
    "phantom_night": (
        AttackDefinition(
            key="phantom_night",
            title="Generation at night",
            story="The meter reports 5.5 kW of solar generation with the sun below "
            "the horizon.",
            expected_detection="night_generation",
            resign=True,
        ),
        _phantom_night,
    ),
    "scaling_drift": (
        AttackDefinition(
            key="scaling_drift",
            title="Subtle 18% drift",
            story="A calibration constant is quietly changed so every reading is 18% "
            "high - small enough to look plausible in isolation, large enough to "
            "matter over a year. No single reading reveals it; the sustained bias does.",
            expected_detection="sustained_bias",
            resign=True,
        ),
        _scaling_drift,
    ),
    "replay": (
        AttackDefinition(
            key="replay",
            title="Replayed reading",
            story="An intercepted high-output reading is resent unchanged. The "
            "signature is perfectly valid because the payload is authentic.",
            expected_detection="sequence",
            resign=False,
        ),
        _replay,
    ),
    "backdate": (
        AttackDefinition(
            key="backdate",
            title="Back-dated reading",
            story="Energy is shifted three hours into the past to land in a "
            "higher-tariff settlement window.",
            expected_detection="timestamp",
            resign=True,
        ),
        _backdate,
    ),
    "tamper_in_transit": (
        AttackDefinition(
            key="tamper_in_transit",
            title="Man-in-the-middle edit",
            story="The value is altered between the device and the server by an "
            "attacker who does not hold the device key.",
            expected_detection="signature",
            resign=False,
        ),
        _tamper_in_transit,
    ),
}


def list_attacks() -> list[dict]:
    return [
        {
            "key": definition.key,
            "title": definition.title,
            "story": definition.story,
            "expected_detection": definition.expected_detection,
            "attacker_holds_device_key": definition.resign,
        }
        for definition, _ in ATTACKS.values()
    ]


def apply_attack(
    attack_key: str, measurement: dict, asset, private_key_hex: str
) -> tuple[dict, str, AttackDefinition]:
    """Return (tampered_measurement, signature, definition).

    If the attacker holds the device key the payload is re-signed, so the
    signature check passes and only physics can catch it. Otherwise the original
    signature is kept, which is what a network attacker would have.
    """
    if attack_key not in ATTACKS:
        raise KeyError(f"unknown attack '{attack_key}'")

    definition, mutate = ATTACKS[attack_key]
    tampered = mutate(measurement, asset)

    if definition.resign:
        signature = sign_measurement(private_key_hex, tampered)
    else:
        signature = sign_measurement(private_key_hex, measurement)

    return tampered, signature, definition
