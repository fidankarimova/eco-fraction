# Eco-Fraction — Stage 1: verifiable generation telemetry

Local prototype of the measurement layer for Eco-Fraction (TEKNOFEST 2026,
Finansal Teknolojiler Yarışması, team VoltLedger).

Stage 1 delivers the bottom of the pipeline: a simulated meter on a solar asset,
a persistence layer, a read API, and a dashboard. No blockchain, no machine
learning, no payments — those sit on top of this and come later.

**Everything here is free and open source.** No cloud account, no credit card, no
API key, no paid service. FastAPI + SQLAlchemy + SQLite on the backend; plain
HTML, CSS and JavaScript on the frontend with no CDN and no build step, so the
dashboard renders with the network unplugged.

---

## The data is modelled, not faked

The readings are not a sine wave with noise sprinkled on it. Each sample runs
through the chain a PV performance model actually uses:

1. **Solar position** — NOAA solar-position equations (declination, equation of
   time, hour angle) for the asset's real latitude and longitude
2. **Clear-sky irradiance** — Haurwitz model for global horizontal irradiance
3. **Diffuse/direct split** — Erbs correlation from the clearness index
4. **Transposition** — beam, isotropic sky diffuse and ground-reflected
   components onto the tilted module plane
5. **Module temperature** — NOCT model from plane-of-array irradiance and ambient
6. **Power** — DC output with a temperature coefficient and soiling loss, then
   inverter efficiency and AC clipping

Cloud cover is deterministic multi-octave value noise keyed on the asset's
weather seed and the timestamp. Determinism is the point: the historical backfill
and the live tick call the same function, so the curve is continuous across a
restart and **any past reading can be recomputed and checked**. That property is
what Stage 2's signing and anchoring will rely on.

The default asset is a 12.5 kWp / 10 kW rooftop array in Baku. On a clear August
day it produces roughly 6.5 kWh/kWp — the right order of magnitude for the site,
which a reviewer can verify against PVGIS.

---

## Requirements

- Python 3.11 or newer (developed on 3.12)
- Nothing else. No Node, no Docker, no database server.

## Setup

```bash
cd eco-fraction
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn backend.main:app --reload --port 8000
```

Then open **http://127.0.0.1:8000**

On first start the app creates `data/ecofraction.db`, seeds the demo asset, and
backfills 7 days of history at 5-minute resolution (about 2,000 readings, under a
second). After that the simulator appends a live reading every 5 seconds.

`make install`, `make run` and `make test` wrap the same commands.

## Test

```bash
python -m pytest -q          # 21 tests
```

The solar tests assert physics rather than magic numbers: zero output at local
midnight, peak elevation matching `90 − latitude + 23.44°` at the solstice, AC
power never exceeding the inverter rating, daily specific yield inside 3–8
kWh/kWp, and a clearness index that moves smoothly second to second.

## Useful commands

```bash
python -m scripts.seed_history --stats                      # inspect the database
python -m scripts.seed_history --days 30 --step-minutes 15  # more history
python -m scripts.seed_history --reset --days 7             # wipe and rebuild
```

---

## API

Interactive docs at `/api/docs`; OpenAPI schema at `/api/openapi.json`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Liveness, environment, stored reading count |
| GET | `/api/v1/assets` | All assets with device counts |
| GET | `/api/v1/assets/{id}` | One asset's nameplate and siting parameters |
| GET | `/api/v1/assets/{id}/summary` | Current power, energy today, lifetime energy, status |
| GET | `/api/v1/assets/{id}/readings/latest` | Most recent raw reading |
| GET | `/api/v1/assets/{id}/readings` | Historical readings — `start`, `end`, `limit`, `order` |
| GET | `/api/v1/assets/{id}/series` | Bucketed series for charting — `window_hours`, `bucket_minutes` |

```bash
curl localhost:8000/api/v1/assets/solar-baku-01/summary
curl "localhost:8000/api/v1/assets/solar-baku-01/series?window_hours=24&bucket_minutes=15"
curl "localhost:8000/api/v1/assets/solar-baku-01/readings?limit=5&order=desc"
```

All timestamps are UTC and explicitly timezone-aware. The dashboard converts to
plant-local time using the asset's stored UTC offset and labels it as such.

---

## Configuration

Every setting is an environment variable prefixed `ECO_`, or a line in `.env`.
Copy `.env.example` to `.env` to change anything. Nothing needs changing to run.

| Variable | Default | Notes |
| --- | --- | --- |
| `ECO_DATABASE_URL` | `sqlite:///./data/ecofraction.db` | Any SQLAlchemy URL |
| `ECO_ENABLE_SIMULATOR` | `true` | Set `false` to freeze the data |
| `ECO_SAMPLE_INTERVAL_SECONDS` | `5` | Live tick cadence |
| `ECO_BACKFILL_DAYS` | `7` | History generated on an empty database |
| `ECO_BACKFILL_STEP_MINUTES` | `5` | Resolution of that history |

To use local PostgreSQL instead of SQLite — also free, also no account:

```bash
pip install "psycopg[binary]"
export ECO_DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/ecofraction"
```

No code changes are needed; the aggregation is done in Python precisely so it
runs identically on both engines.

---

## Project layout

```
eco-fraction/
├── backend/
│   ├── main.py                 FastAPI factory, lifespan, static mount
│   ├── config.py               Settings (ECO_* env vars)
│   ├── database.py             Engine + session management
│   ├── models.py               Asset, Device, Reading + UtcDateTime
│   ├── schemas.py              API response contract
│   ├── services.py             Queries, summary, series bucketing
│   ├── api/routes.py           HTTP handlers
│   └── simulation/
│       ├── solar.py            The PV physics model
│       └── device.py           Meter, backfill, live loop
├── frontend/
│   ├── index.html              Dashboard markup
│   ├── styles.css              Instrument-panel visual system
│   └── app.js                  Polling + hand-drawn SVG chart
├── scripts/seed_history.py     History seeder and inspector
├── tests/                      21 tests (physics + API contract)
├── requirements.txt
└── .env.example
```

---

## Free substitutions for things that normally cost money

Recorded now so the final report can state them honestly rather than implying a
production stack exists.

| Production component | Stage 1 substitute | Production alternative |
| --- | --- | --- |
| AWS IoT Core ingest | In-process simulated device writing directly to the DB | Self-hosted Mosquitto or EMQX (both open source) before any managed broker |
| Apache Kafka stream | Not used — unnecessary at this throughput | Redpanda or Kafka once device count justifies it |
| Managed time-series database | Local SQLite | Local PostgreSQL + TimescaleDB, both open source |
| Physical revenue-grade meter | Physics-based simulated meter | ESP32 + INA219 shunt, or the inverter's own local API |
| Paid weather/irradiance API | Deterministic in-process cloud model | Open-Meteo, NASA POWER or PVGIS — all free, no key |
| Paid blockchain RPC | Not yet used | Local Anvil node first, then a public Polygon Amoy endpoint |
| USDC settlement | Not yet used | Mock ERC-20 on a local chain; never real funds |
| Commercial KYC vendor | Not yet used | Mock registry contract with a whitelist |

## Known gaps, stated deliberately

- **The grid emission factor is a placeholder** (0.58 kg CO₂e/kWh). The dashboard
  labels the CO₂ figure `unverified factor` on purpose. It must be replaced with
  a cited national figure before the number appears in the report or on a slide.
- **The device is simulated and says so.** Stage 1 makes no claim of verified
  data. Signing, cross-validation against independent reference data, and Merkle
  anchoring are Stage 2 — and the `sequence` and `payload_hash` columns already
  exist so that work needs no migration.
- **Lifetime energy means "since monitoring began"**, not since the array was
  commissioned. The dashboard labels it that way.

## Licence

Prototype for a student competition entry. Dependencies are MIT, BSD or
Apache-2.0.
