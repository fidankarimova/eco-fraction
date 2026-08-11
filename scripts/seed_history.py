"""Seed or extend the local reading history.

The API backfills automatically on an empty database, so this script is for when
you want a different amount of history, or want to inspect what is stored.

    python -m scripts.seed_history --days 30 --step-minutes 15
    python -m scripts.seed_history --stats
    python -m scripts.seed_history --reset --days 7
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.database import Database  # noqa: E402
from backend.models import Base, Reading  # noqa: E402
from backend.services import ensure_seed_asset  # noqa: E402
from backend.simulation.device import backfill_history  # noqa: E402


def print_stats(db: Database) -> None:
    with db.session() as session:
        row = session.execute(
            select(
                func.count(Reading.id),
                func.min(Reading.recorded_at),
                func.max(Reading.recorded_at),
                func.coalesce(func.max(Reading.cumulative_energy_wh), 0.0),
                func.coalesce(func.max(Reading.ac_power_w), 0.0),
            )
        ).one()

    count, first, last, total_wh, peak_w = row
    print(f"readings          : {count:,}")
    print(f"first reading     : {first}")
    print(f"last reading      : {last}")
    print(f"cumulative energy : {total_wh / 1000:.2f} kWh")
    print(f"peak AC power     : {peak_w / 1000:.2f} kW")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eco-Fraction history seeder")
    parser.add_argument("--days", type=int, default=7, help="days of history to generate")
    parser.add_argument(
        "--step-minutes", type=int, default=5, help="resolution of the generated history"
    )
    parser.add_argument(
        "--reset", action="store_true", help="drop and recreate all tables first"
    )
    parser.add_argument("--stats", action="store_true", help="only print statistics")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    settings = get_settings()
    db = Database(settings)

    if args.reset:
        Base.metadata.drop_all(db.engine)
        print("dropped all tables")

    db.create_all()
    with db.session() as session:
        ensure_seed_asset(session)

    if not args.stats:
        started = datetime.now(tz=timezone.utc)
        written = backfill_history(db, days=args.days, step_minutes=args.step_minutes)
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        print(f"wrote {written:,} readings in {elapsed:.2f}s")

    print_stats(db)
    db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
