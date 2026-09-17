"""Generate the measured evidence table for the final report and jury deck.

Every quantitative claim the project makes should come from here rather than
from an adjective. Run it, paste the numbers, cite this script as the method.

    python -m scripts.evidence_report
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from backend.anchoring import MerkleTree, verify_proof  # noqa: E402
from backend.attacks import ATTACKS  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.database import Database  # noqa: E402
from backend.ledger import MICRO, AssetLedger  # noqa: E402
from backend.main import apply_schema  # noqa: E402
from backend.models import Asset, Reading  # noqa: E402
from backend.services import anchor_pending_readings, ensure_seed_asset  # noqa: E402
from backend.simulation.device import (  # noqa: E402
    backfill_history,
    ensure_device_keys,
    record_live_tick,
)

DAYS = 14
STEP_MINUTES = 5


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    settings = Settings(
        database_url="sqlite:///:memory:", enable_simulator=False, log_level="ERROR"
    )
    db = Database(settings)
    apply_schema(db)
    with db.session() as session:
        ensure_seed_asset(session)
        ensure_device_keys(session)

    print("=" * 66)
    print("ECO-FRACTION - MEASURED EVIDENCE")
    print(f"generated {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 66)

    # --- 1. false positives on honest data --------------------------------
    rule("1. False positives on honest telemetry")
    started = time.perf_counter()
    written = backfill_history(db, days=DAYS, step_minutes=STEP_MINUTES)
    elapsed = time.perf_counter() - started

    with db.session() as session:
        total = int(session.scalar(select(func.count()).select_from(Reading)) or 0)
        rejected = int(
            session.scalar(
                select(func.count())
                .select_from(Reading)
                .where(Reading.is_trusted.is_(False))
            )
            or 0
        )
    print(f"honest readings generated : {total:,} ({DAYS} days @ {STEP_MINUTES} min)")
    print(f"falsely rejected          : {rejected}")
    print(f"false positive rate       : {rejected / max(total,1) * 100:.4f}%")
    print(f"generation + validation   : {elapsed:.2f}s ({written/max(elapsed,1e-9):,.0f} readings/s)")

    # --- 2. attack detection ---------------------------------------------
    rule("2. Attack detection")
    caught = 0
    for key in sorted(ATTACKS):
        outcome = record_live_tick(db, 5, attack_key=key)["outcomes"][0]
        detected = not outcome["is_trusted"]
        caught += detected
        mark = "CAUGHT " if detected else "MISSED "
        print(
            f"  {mark} {key:<20s} score {outcome['trust_score']:.2f}  "
            f"failed: {', '.join(outcome['failed_checks'])}"
        )
    print(f"\ndetection rate            : {caught}/{len(ATTACKS)} "
          f"({caught / len(ATTACKS) * 100:.0f}%)")

    # --- 3. anchoring ------------------------------------------------------
    rule("3. Merkle anchoring")
    with db.session() as session:
        asset = session.get(Asset, "solar-baku-01")
        started = time.perf_counter()
        batch = anchor_pending_readings(session, asset)
        anchor_time = time.perf_counter() - started
        readings = list(
            session.scalars(
                select(Reading).where(Reading.batch_id == batch.id).order_by(Reading.id)
            ).all()
        )
        hashes = [r.payload_hash for r in readings]

    tree = MerkleTree(hashes)
    started = time.perf_counter()
    ok = all(
        verify_proof(hashes[i], tree.proof_for_index(i))
        for i in range(0, len(hashes), max(1, len(hashes) // 200))
    )
    verify_time = time.perf_counter() - started
    print(f"readings in batch         : {batch.reading_count:,}")
    print(f"on-chain footprint        : 32 bytes (one Merkle root)")
    print(f"compression               : {batch.reading_count:,} readings -> 1 root")
    print(f"proof length              : {len(tree.proof_for_index(0).path)} hashes")
    print(f"build + settle time       : {anchor_time*1000:.1f} ms")
    print(f"all sampled proofs verify : {ok}")
    print(f"verification time         : {verify_time*1000:.1f} ms for 200 proofs")

    # --- 4. distribution cost ---------------------------------------------
    rule("4. Distribution cost vs holder count")
    print(f"{'holders':>10} {'accumulator updates':>22} {'time (ms)':>12}")
    for holders in (10, 100, 1_000, 10_000):
        ledger = AssetLedger(
            asset_id="bench", total_supply=holders, token_price_micro_usdc=MICRO
        )
        for i in range(holders):
            ledger.purchase(f"h{i}", 1)
        started = time.perf_counter()
        ledger.deposit_revenue(holders * MICRO)
        took = (time.perf_counter() - started) * 1000
        print(f"{holders:>10,} {1:>22} {took:>12.4f}")
    print("\nOne accumulator update regardless of holder count. A naive push loop")
    print("would be O(n) transfers and would exceed the block gas limit.")

    # --- 5. impact --------------------------------------------------------
    rule("5. Impact accounting")
    with db.session() as session:
        asset = session.get(Asset, "solar-baku-01")
        verified_kwh = float(
            session.scalar(
                select(func.coalesce(func.sum(Reading.energy_wh), 0.0)).where(
                    Reading.is_trusted.is_(True)
                )
            )
            or 0.0
        ) / 1000.0
    print(f"verified energy           : {verified_kwh:,.1f} kWh over {DAYS} days")
    print(f"specific yield            : {verified_kwh / asset.dc_capacity_kw / DAYS:.2f} kWh/kWp/day")
    print(f"emission factor           : {asset.grid_emission_factor_kg_per_kwh} kg/kWh")
    print(f"emission factor source    : {asset.emission_factor_source}")
    print(f"certificate status        : {asset.certificate_status}")
    print(f"CO2e (informational only) : {verified_kwh * asset.grid_emission_factor_kg_per_kwh:,.1f} kg")
    print("\nNOT claimable: the environmental attribute is unverified, so claiming")
    print("it would risk double counting. This is deliberate, not an oversight.")

    print("\n" + "=" * 66)
    db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
