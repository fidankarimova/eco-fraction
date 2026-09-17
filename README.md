# Eco-Fraction — verifiable generation telemetry and fractional revenue

Local prototype for Eco-Fraction (TEKNOFEST 2026, Finansal Teknolojiler
Yarışması, team VoltLedger).

The pipeline, end to end, in the order that makes the claims true:

    sense → sign → validate → anchor → account → distribute

A simulated meter measures a solar array with a physical PV model, signs each
reading at the edge with Ed25519, and submits it. Eight independent checks decide
whether the reading may be trusted. Trusted readings are batched into a Merkle
tree and anchored. Only anchored, verified energy produces an impact record and
revenue, and that revenue accrues to fractional token holders who pull their
share.

**Trust is a precondition of payment, not a badge on a dashboard.** An untrusted
reading contributes no energy, no CO₂ and no money — and you can prove it live by
pressing a button that tampers with the meter.

**Everything here is free and open source.** No cloud account, no credit card, no
API key, no paid service. FastAPI + SQLAlchemy + SQLite on the backend; plain
HTML, CSS and JavaScript on the frontend with no CDN and no build step, so the
dashboard renders with the network unplugged.

---

## Measured results

Reproduce all of these with `python -m scripts.evidence_report`:

| What | Result |
| --- | --- |
| Attacks in the suite detected | **6 / 6 (100%)** |
| False positives on honest telemetry | **0 of 4,033 (0.0000%)** |
| Ingest + validation throughput | ~2,300 readings/s on one laptop core |
| On-chain footprint | 4,038 readings → **one 32-byte Merkle root** |
| Inclusion proof length | 12 hashes |
| Proof verification | 200 proofs in 2.7 ms |
| Distribution cost at 10,000 holders | **1 accumulator update**, 0.31 ms |
| Specific yield (Baku, August) | 5.24 kWh/kWp/day |

## The oracle problem, answered by demonstration

Blockchain proves a number was not changed *after* it was written. It says
nothing about whether the number was true when written. If the plant owner
controls the sensor, they control your "verified" data.

So validation uses three independent kinds of check, because no single compromise
defeats all three:

| Kind | Checks | Catches |
| --- | --- | --- |
| Cryptographic | signature, sequence, timestamp | edited, replayed, back-dated readings |
| Physical | night generation, clear-sky ceiling, ramp rate | values no real array could produce — *even when correctly signed by a compromised device* |
| Cross-reference | reference consistency, sustained bias | a device reporting internally-consistent fiction |

A **replay** attack is physically perfect and only cryptography sees it. An
**inflated output** attack is perfectly signed and only physics sees it. That is
why both layers exist.

The dashboard has a "Try to cheat the meter" panel with six real manipulations.
Press one and watch the reading get rejected, the failed checks light up, and the
energy drop out of the revenue calculation. Hand it to a juror.

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

**Telemetry**

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Liveness, environment, reading count, schema version |
| GET | `/api/v1/assets` | All assets with device counts |
| GET | `/api/v1/assets/{id}` | Nameplate, siting and emission-factor provenance |
| GET | `/api/v1/assets/{id}/summary` | Power, energy, status, trust score, claimability |
| GET | `/api/v1/assets/{id}/readings/latest` | Most recent reading with its signature and verdict |
| GET | `/api/v1/assets/{id}/readings` | History — `start`, `end`, `limit`, `order` |
| GET | `/api/v1/assets/{id}/series` | Bucketed series — `window_hours`, `bucket_minutes` |

**Verification**

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/assets/{id}/trust` | Trust rate, failing checks, latest anchor, attack log |
| POST | `/api/v1/assets/{id}/anchor` | Batch, Merkle-root and settle un-anchored readings |
| GET | `/api/v1/assets/{id}/batches` | Anchored batch history |
| GET | `/api/v1/verify/reading/{id}` | **Public proof** that a reading is in an anchored batch |
| GET | `/api/v1/verify/batch/{root}` | Recompute a root, with its revenue and impact |
| GET | `/api/v1/attacks` | The attack catalogue |
| POST | `/api/v1/assets/{id}/attacks/{key}` | Inject a manipulation and see the verdict |

**Tokenisation and impact**

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/assets/{id}/token` | Supply, price, holders, undistributed revenue |
| GET | `/api/v1/assets/{id}/token/holders/{address}` | One holder's stake and claimable balance |
| POST | `/api/v1/assets/{id}/token/purchase` | Buy fractional tokens (mock USDC) |
| POST | `/api/v1/assets/{id}/token/claim` | Pull accrued revenue — O(1) per holder |
| GET | `/api/v1/assets/{id}/revenue` | Gross, platform fee, O&M, net, distributed |
| GET | `/api/v1/assets/{id}/impact` | Deterministic CO₂ accounting with certificate status |

```bash
curl localhost:8000/api/v1/assets/solar-baku-01/summary
curl localhost:8000/api/v1/assets/solar-baku-01/trust
curl -X POST localhost:8000/api/v1/assets/solar-baku-01/attacks/inflate_output
curl localhost:8000/api/v1/verify/reading/1
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

| Production component | Prototype substitute | Production alternative |
| --- | --- | --- |
| AWS IoT Core ingest | In-process simulated device writing directly to the DB | Self-hosted Mosquitto or EMQX (both open source) before any managed broker |
| Apache Kafka stream | Not used — unnecessary at this throughput | Redpanda or Kafka once device count justifies it |
| Managed time-series database | Local SQLite | Local PostgreSQL + TimescaleDB, both open source |
| Physical revenue-grade meter | Physics-based simulated meter | ESP32 + INA219 shunt, or the inverter's own local API |
| Paid weather/irradiance API | Deterministic in-process cloud model | Open-Meteo, NASA POWER or PVGIS — all free, no key |
| Paid blockchain RPC | Merkle roots in a local append-only anchor log, labelled `local-anchor` | `AnchorRegistry` on Polygon Amoy via a free public RPC; the cryptography does not change |
| ERC-1155 token contract | `backend/ledger.py`, an executable specification of the same semantics | Solidity contract with the transfer hook; the Python tests become the contract tests |
| USDC settlement | Mock USDC integers on a local ledger; **no real funds** | Test USDC on Amoy. Real settlement needs a licence and is out of scope |
| Commercial KYC vendor | `kyc_verified` flag on the holder record | Same gate as a Solidity transfer hook against a KYC registry |
| Paid ML platform (XGBoost/LSTM) | Deterministic physics + statistical bias detection, measured against honest data | Gradient boosting on the residuals once labelled fault data exists — see "Known gaps" |

## Known gaps, stated deliberately

- **The grid emission factor is a placeholder** (0.58 kg CO₂e/kWh). The dashboard
  labels the CO₂ figure `unverified factor` on purpose. It must be replaced with
  a cited national figure before the number appears in the report or on a slide.
- **The device is simulated and says so.** Signing and validation prove the data
  path is tamper-evident; they cannot prove a simulated array exists. The claim
  this prototype supports is "manipulation of the telemetry path is detectable",
  not "this plant produced this energy".
- **The independent reference is a clear-sky model, not satellite data.** It
  bounds what is physically possible and catches sustained over-reporting. A
  production system cross-checks against satellite irradiance, the inverter's own
  counter and the settlement meter. Free sources exist (Open-Meteo, NASA POWER,
  PVGIS) and are the next honest upgrade.
- **No machine learning model is trained yet.** The submitted report named
  XGBoost and LSTM. Those need labelled fault data and a baseline to beat; the
  physics and statistical checks are the correct baseline, and any model must be
  shown to beat them before it earns a place. Claiming a trained model here would
  be the kind of unsupported assertion this project exists to avoid.
- **The anchor is local.** Merkle roots are real and proofs verify, but they are
  recorded in a local log, not on Polygon. Everything the chain will store is
  already computed; only the destination changes.
- **Lifetime energy means "since monitoring began"**, not since the array was
  commissioned. The dashboard labels it that way.

## Licence

Prototype for a student competition entry. Dependencies are MIT, BSD or
Apache-2.0.
