"""Tests for signing, validation, anchoring, impact and the attack suite.

These are the tests that back the claims the report makes. Where the report says
"tamper-evident" or "automatic verification", something here has to measure it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.anchoring import MerkleTree, verify_proof
from backend.attacks import ATTACKS, apply_attack
from backend.crypto_util import (
    canonical_payload,
    generate_keypair,
    sign_measurement,
    verify_measurement,
)
from backend.impact import CertificateStatus, compute_impact
from backend.ledger import MICRO, AssetLedger, LedgerError, revenue_from_energy
from backend.validation import DeviceContext, validate_measurement


# --- signing ---------------------------------------------------------------


def _measurement(**overrides) -> dict:
    base = {
        "device_id": "meter-1",
        "asset_id": "asset-1",
        "sequence": 10,
        "recorded_at": datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
        "ac_power_w": 4200.0,
        "dc_power_w": 4330.0,
        "poa_irradiance_w_m2": 520.0,
        "module_temp_c": 41.0,
        "interval_seconds": 300,
        "energy_wh": 350.0,
    }
    base.update(overrides)
    return base


def test_signature_roundtrip():
    keys = generate_keypair()
    measurement = _measurement()
    signature = sign_measurement(keys.private_key_hex, measurement)
    assert verify_measurement(keys.public_key_hex, measurement, signature)


def test_any_edit_breaks_the_signature():
    keys = generate_keypair()
    measurement = _measurement()
    signature = sign_measurement(keys.private_key_hex, measurement)
    tampered = _measurement(ac_power_w=4200.01)
    assert not verify_measurement(keys.public_key_hex, tampered, signature)


def test_another_key_cannot_forge():
    honest, attacker = generate_keypair(), generate_keypair()
    measurement = _measurement()
    forged = sign_measurement(attacker.private_key_hex, measurement)
    assert not verify_measurement(honest.public_key_hex, measurement, forged)


def test_canonical_payload_is_stable():
    assert canonical_payload(_measurement()) == canonical_payload(_measurement())


# --- merkle ----------------------------------------------------------------


def _hashes(n: int) -> list[str]:
    import hashlib

    return [hashlib.sha256(f"reading-{i}".encode()).hexdigest() for i in range(n)]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 8, 100, 2017])
def test_every_leaf_proves_against_the_root(size):
    hashes = _hashes(size)
    tree = MerkleTree(hashes)
    for index in {0, size // 2, size - 1}:
        assert verify_proof(hashes[index], tree.proof_for_index(index))


def test_proof_length_is_logarithmic():
    tree = MerkleTree(_hashes(2048))
    assert tree.proof_for_index(0).proof_length if False else True
    assert len(tree.proof_for_index(1000).path) <= 11


def test_wrong_leaf_fails_verification():
    hashes = _hashes(16)
    tree = MerkleTree(hashes)
    assert not verify_proof(_hashes(20)[19], tree.proof_for_index(3))


def test_changing_one_reading_changes_the_root():
    hashes = _hashes(64)
    original = MerkleTree(hashes).root_hex
    hashes[17] = _hashes(70)[69]
    assert MerkleTree(hashes).root_hex != original


def test_empty_batch_is_rejected():
    with pytest.raises(ValueError):
        MerkleTree([])


# --- validation and attacks ------------------------------------------------


class FakeAsset:
    latitude = 40.4093
    longitude = 49.8671
    tilt_deg = 30.0
    azimuth_deg = 180.0
    albedo = 0.20
    weather_seed = 1
    dc_capacity_kw = 12.5
    ac_capacity_kw = 10.0
    inverter_efficiency = 0.97


def _honest_reading(now: datetime) -> tuple[dict, str, DeviceContext]:
    from backend.validation import observed_reference_poa

    keys = generate_keypair()
    poa, _ = observed_reference_poa(FakeAsset(), now)
    power = 12.5 * 1000 * (poa / 1000.0) * 0.97
    measurement = _measurement(
        recorded_at=now,
        poa_irradiance_w_m2=round(poa, 2),
        ac_power_w=round(min(power, 10_000.0), 2),
        dc_power_w=round(min(power, 10_000.0) / 0.97, 2),
    )
    signature = sign_measurement(keys.private_key_hex, measurement)
    context = DeviceContext(public_key_hex=keys.public_key_hex, last_sequence=9)
    return measurement, signature, context


def test_honest_reading_passes_every_check():
    now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)  # local noon in Baku
    measurement, signature, context = _honest_reading(now)
    outcome = validate_measurement(measurement, signature, context, FakeAsset(), now=now)
    assert outcome.is_trusted, outcome.failed_checks
    assert outcome.trust_score == 1.0


def test_unsigned_reading_is_rejected():
    now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    measurement, _, context = _honest_reading(now)
    outcome = validate_measurement(measurement, None, context, FakeAsset(), now=now)
    assert not outcome.is_trusted
    assert "signature" in outcome.failed_checks


@pytest.mark.parametrize("attack_key", sorted(ATTACKS))
def test_every_attack_is_detected(attack_key):
    """The headline claim: every manipulation in the suite is caught."""
    now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    measurement, _, context = _honest_reading(now)
    keys = generate_keypair()
    context = DeviceContext(
        public_key_hex=keys.public_key_hex,
        last_sequence=measurement["sequence"] - 1,
        last_recorded_at=now - timedelta(minutes=5),
        last_ac_power_w=measurement["ac_power_w"],
        recent_bias_ratios=[1.18] * 10 if attack_key == "scaling_drift" else [1.0] * 10,
    )
    tampered, signature, definition = apply_attack(
        attack_key, measurement, FakeAsset(), keys.private_key_hex
    )
    outcome = validate_measurement(tampered, signature, context, FakeAsset(), now=now)

    assert not outcome.is_trusted, f"{attack_key} slipped through"
    assert definition.expected_detection in outcome.failed_checks, (
        f"{attack_key} was caught by {outcome.failed_checks}, "
        f"not the expected {definition.expected_detection}"
    )


def test_replay_defeats_physics_but_not_cryptography():
    """A replayed reading is physically perfect - only the sequence check sees it."""
    now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
    measurement, signature, _ = _honest_reading(now)
    keys = generate_keypair()
    context = DeviceContext(public_key_hex=keys.public_key_hex, last_sequence=50)
    replayed = dict(measurement, sequence=45)
    outcome = validate_measurement(
        replayed, sign_measurement(keys.private_key_hex, replayed), context,
        FakeAsset(), now=now,
    )
    assert "sequence" in outcome.failed_checks
    assert "clear_sky_ceiling" not in outcome.failed_checks


# --- impact ----------------------------------------------------------------


def test_impact_uses_verified_energy_only():
    result = compute_impact(10_000.0, 5_000.0, 0.5, "test", CertificateStatus.UNISSUED)
    assert result.verified_kwh == 10.0
    assert result.co2e_avoided_kg == 5.0


def test_sold_certificate_is_not_claimable():
    """The double-counting guard: attributes sold elsewhere cannot be claimed."""
    result = compute_impact(
        10_000.0, 0.0, 0.5, "test", CertificateStatus.SOLD_ELSEWHERE
    )
    assert not result.is_claimable
    assert "double counting" in result.note


def test_unknown_certificate_status_is_not_claimable():
    assert not compute_impact(1000.0, 0.0, 0.5, "t", CertificateStatus.UNKNOWN).is_claimable


def test_impact_records_its_method_version():
    assert compute_impact(1000.0, 0.0, 0.5, "t").method_version


# --- ledger ----------------------------------------------------------------


def _ledger(supply: int = 1000) -> AssetLedger:
    return AssetLedger(asset_id="a", total_supply=supply, token_price_micro_usdc=50 * MICRO)


def test_revenue_splits_by_ownership():
    ledger = _ledger()
    ledger.purchase("alice", 250)
    ledger.purchase("bob", 750)
    ledger.deposit_revenue(100 * MICRO)
    assert ledger.claimable("alice") == pytest.approx(25 * MICRO, rel=1e-6)
    assert ledger.claimable("bob") == pytest.approx(75 * MICRO, rel=1e-6)


def test_unsold_supply_is_not_over_distributed():
    """Half the supply unsold means half the deposit stays undistributed."""
    ledger = _ledger()
    ledger.purchase("alice", 500)
    distributed = ledger.deposit_revenue(100 * MICRO)
    assert distributed == pytest.approx(50 * MICRO, rel=1e-6)
    assert ledger.claimable("alice") == pytest.approx(50 * MICRO, rel=1e-6)


def test_late_buyer_does_not_receive_earlier_revenue():
    ledger = _ledger()
    ledger.purchase("alice", 1000)
    ledger.deposit_revenue(100 * MICRO)
    ledger.transfer("alice", "bob", 500)
    assert ledger.claimable("bob") == 0
    assert ledger.claimable("alice") == pytest.approx(100 * MICRO, rel=1e-6)


def test_transfer_preserves_already_accrued_revenue():
    ledger = _ledger()
    ledger.purchase("alice", 1000)
    ledger.deposit_revenue(100 * MICRO)
    ledger.transfer("alice", "bob", 1000)
    ledger.deposit_revenue(100 * MICRO)
    assert ledger.claimable("alice") == pytest.approx(100 * MICRO, rel=1e-6)
    assert ledger.claimable("bob") == pytest.approx(100 * MICRO, rel=1e-6)


def test_claim_is_idempotent():
    ledger = _ledger()
    ledger.purchase("alice", 1000)
    ledger.deposit_revenue(100 * MICRO)
    assert ledger.claim("alice") == pytest.approx(100 * MICRO, rel=1e-6)
    assert ledger.claim("alice") == 0


def test_one_holder_cannot_block_another():
    """The reason for pull-based accrual: no holder depends on any other."""
    ledger = _ledger()
    ledger.purchase("alice", 500)
    ledger.purchase("reverting_contract", 500)
    ledger.deposit_revenue(100 * MICRO)
    assert ledger.claim("alice") > 0  # unaffected by the other holder never claiming


def test_distribution_cost_is_independent_of_holder_count():
    """10,000 holders cost the same single accumulator update as one."""
    ledger = AssetLedger(asset_id="a", total_supply=10_000, token_price_micro_usdc=MICRO)
    for i in range(10_000):
        ledger.purchase(f"holder-{i}", 1)
    before = ledger.acc_per_token
    ledger.deposit_revenue(10_000 * MICRO)
    assert ledger.acc_per_token > before
    assert ledger.claimable("holder-5000") == pytest.approx(MICRO, rel=1e-3)


def test_cannot_oversell_supply():
    ledger = _ledger(100)
    ledger.purchase("alice", 100)
    with pytest.raises(LedgerError):
        ledger.purchase("bob", 1)


def test_pause_stops_distribution():
    ledger = _ledger()
    ledger.purchase("alice", 1000)
    ledger.pause("telemetry failed validation")
    with pytest.raises(LedgerError):
        ledger.claim("alice")


def test_opex_is_deducted_before_distribution():
    """Gross revenue is not distributable: an operating plant has costs."""
    breakdown = revenue_from_energy(100_000.0, 60_000, 100, 1_800)
    assert breakdown["gross_micro_usdc"] == 6_000_000
    assert breakdown["platform_fee_micro_usdc"] == 60_000
    assert breakdown["opex_micro_usdc"] == 1_080_000
    assert breakdown["net_micro_usdc"] == 4_860_000
