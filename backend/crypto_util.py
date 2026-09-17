"""Device identity and payload signing.

Answers the question a jury will ask first: *if the plant owner controls the
sensor, what stops them from lying?*

Each device holds an Ed25519 private key. Every reading is signed at the edge over
a canonical JSON payload. The server holds only the public key, so a reading whose
signature verifies could only have been produced by that device's key - a value
edited anywhere between the device and the database fails verification.

This does not make the *measurement* honest (a compromised device signs lies
perfectly well). It makes the measurement **attributable and tamper-evident**,
which is the part blockchain alone cannot give you. Physical plausibility is
checked separately in :mod:`backend.validation`.

Ed25519 via ``cryptography`` (Apache-2.0 / BSD): free, open source, no account.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

CANONICAL_FIELDS = (
    "device_id",
    "asset_id",
    "sequence",
    "recorded_at",
    "ac_power_w",
    "dc_power_w",
    "poa_irradiance_w_m2",
    "module_temp_c",
    "interval_seconds",
    "energy_wh",
)


@dataclass(frozen=True)
class DeviceKeypair:
    private_key_hex: str
    public_key_hex: str


def generate_keypair() -> DeviceKeypair:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return DeviceKeypair(private_bytes.hex(), public_bytes.hex())


def canonical_payload(measurement: dict) -> str:
    """Deterministic JSON over exactly the signed fields.

    Both sides must agree byte for byte, so key order is fixed, floats are rounded
    to a declared precision and datetimes use ISO-8601. Any drift here shows up as
    a signature failure rather than silent acceptance.
    """
    ordered: dict[str, object] = {}
    for field in CANONICAL_FIELDS:
        value = measurement[field]
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, float):
            value = round(value, 6)
        ordered[field] = value
    return json.dumps(ordered, separators=(",", ":"), sort_keys=False)


def sign_measurement(private_key_hex: str, measurement: dict) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    payload = canonical_payload(measurement).encode("utf-8")
    return private_key.sign(payload).hex()


def verify_measurement(public_key_hex: str, measurement: dict, signature_hex: str) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        payload = canonical_payload(measurement).encode("utf-8")
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
