# AirPower: integrated predictive maintenance and fleet availability

Prototype for **SIH 2026 problem statement 26249**, *Air Power: Predictive Maintenance & Fleet
Availability* (Ministry of Defence, Defence Services Staff College). Plan:
[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

The problem statement says aircraft availability is low because maintenance is reactive and the data
that could make it predictive sits in four systems that are not integrated: aircraft health
monitoring (HUMS), technical records, spares, and maintenance agencies. This repository builds the
integrated platform that joins them, predicts, and plans, and proves its value on a synthetic fleet.

**Status: Phases 0-8 done** (setup, fleet simulator and data model, integration layer with API,
health models on real data with fleet risk records, records NLP on real FAA defect reports and
reliability analytics, spares optimisation and maintenance planning, the predictive policy tested in
the simulated fleet with a value-of-prediction curve and a fleet twin for what-if forecasts, the
full dashboard, and hardening: roles, audit log, TLS, containers, security and operations
documents). Results: [docs/VALIDATION.md](docs/VALIDATION.md). Security model and its gaps:
[docs/SECURITY.md](docs/SECURITY.md). Install, run, back up: [docs/OPERATIONS.md](docs/OPERATIONS.md).
A 10-minute demo script: [docs/DEMO.md](docs/DEMO.md).

## What is real and what is synthetic

* The **fleet** (48 aircraft of two generic types, two bases, a depot) is simulated by FleetSim. No
  real military data is used, and the types do not represent any in-service aircraft.
* The **HUMS signals inside it are real or NASA data**: engines replay NASA C-MAPSS run-to-failure
  trajectories (units held out from any model training), helicopter gearboxes replay CWRU / SEU
  vibration windows, batteries follow NASA capacity-fade curves.
* The four **source systems are deliberately fragmented**: different aircraft keys, time zones,
  date formats, part numbering, plus injected record faults. Each fault is logged, so the
  integration layer is scored against truth rather than judged by eye.

## Quick start (Windows, Python 3.13, Node 24)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pip install --no-deps --ignore-requires-python progpy==1.7.1
.venv/Scripts/python -m pip install --no-deps --no-build-isolation filterpy==1.4.5

# 1. simulate one year -> outputs/fleetsim/p0_s1_365d/{sources,truth}
.venv/Scripts/python -m airpower.fleetsim run --days 365 --seed 1

# 2. integrate the four sources into the store (SQLite by default; AIRPOWER_DB for PostgreSQL)
.venv/Scripts/python -m airpower.ingest.load outputs/fleetsim/p0_s1_365d

# 3. score the integration against the simulator's truth
.venv/Scripts/python -m tools.reconcile outputs/fleetsim/p0_s1_365d

# 4. dashboard + API on http://127.0.0.1:8000. Sign-in is on: --demo-users creates demo accounts
#    (deploy/demo_users.yaml: commander, engineer, logistics, analyst, admin; demos and tests only).
#    A real first start uses AIRPOWER_ADMIN_PASSWORD_FILE and AIRPOWER_SECRET_FILE (docs/OPERATIONS.md).
(cd web && npm install && npm run build)
.venv/Scripts/python -m server.app --demo-users

# Phase 3: health models on real data, fleet risk records, docs/VALIDATION.md (about 15 min)
bash tools/phase3_pipeline.sh

# Phase 4: FAA defect reports, ATA classifier, reliability / rogue serials / repeat defects (about 15 min)
bash tools/phase4_pipeline.sh

# Phase 5: spares forecast backtest, METRIC allocation and its validation, AOG risk, maintenance plans (about 10 min)
bash tools/phase5_pipeline.sh

# Phases 6-7: engine model with mid-life starts, policy study P0 / P1 / P2 (30 paired years per arm,
# resumable), value of prediction, fleet twin forecast check, dashboard forecast (about 1.5 hours)
bash tools/phase6_pipeline.sh

# one what-if forecast from the store's current state (also in the dashboard's What if tab)
.venv/Scripts/python -m airpower.twin.whatif run --levers '{"flying": 1.3, "policy": "P2"}' --days 90

# tests (dataset tests skip when AeroTwin's data is absent)
.venv/Scripts/python -m pytest -q

# prove this README from nothing: fresh copy, fresh environment, tests, simulate, integrate, serve, sign in (about 10 min)
bash tools/clean_install_check.sh

# containers (written and configuration-checked; see docs/OPERATIONS.md section 6 before relying on them)
.venv/Scripts/python -m tools.init_deploy --demo-cert
docker compose -f deploy/docker-compose.yml --profile tls up -d --build
```

Real datasets: C-MAPSS is in `data/raw/cmapss`; NGAFID, CWRU / SEU and NASA battery are read in
place from AeroTwin's prepared data (`AEROTWIN_DATA`, default `F:/AeroTwin/sih26054_v2/data`).
`python -m data_prep.sources` reports what is available. Without them, FleetSim falls back to
parametric signals (`--no-real-signals`, used in CI).

## Repository map

| Path | Content |
|---|---|
| `airpower/fleetsim/` | Fleet simulator (SimPy): `config/*.yaml` fleet and component trees, `engine.py` flying / failures / maintenance (and a start from recorded state), `stores.py` two-echelon spares with kits, `signals.py` real-data HUMS, `policies.py` P0 / P1, `predictive.py` P2 with the platform's models in the loop, `exports.py` four fragmented source systems, `truth.py` truth tables and KPIs, `study.py` P0 / P1 comparison |
| `airpower/model/` | Unified data model (SQLAlchemy; SQLite or PostgreSQL / TimescaleDB) |
| `airpower/ingest/` | `registry.yaml` source conventions, `resolve.py` entity resolution, `pipeline.py` integration, `load.py` loader, `secure.py` signed ingest envelope |
| `airpower/twin/` | `status.py` serviceability derived from integrated records, `study.py` Phase 6 policy study and value of prediction, `whatif.py` fleet twin started from the store (forecasts, levers, forecast check), `views.py` health map, aircraft twin and recommended actions |
| `airpower/health/` | `engine_rul.py` (A1), `premaint.py` (A2), `gearbox.py` (A3), `battery.py` (A4), `fleet_risk.py` (A5 + risk records); reused from AeroTwin: `detect.py`, `diagnose.py`, `rul_pf.py`, `vibration.py` |
| `airpower/nlp/` | `ata.py` (A7: defect text -> ATA chapter, trained on FAA SDRs) |
| `airpower/reliability/` | `lifedata.py`, `weibull.py`, `analyse.py` (A6 / A8 / A9), `sdr_repeats.py` (A8 on real reports) |
| `airpower/spares/` | `forecast.py` (prediction-driven demand vs SBA), `metric.py` (METRIC allocation), `validate.py` (in fresh simulated fleets), `actions.py` (AOG risk) |
| `airpower/plan/` | `planner.py` (CP-SAT maintenance planner vs the due-date rule) |
| `data_prep/` | Dataset loaders, C-MAPSS train / fleet-replay split |
| `server/` | FastAPI: readiness, availability, aircraft and aircraft twin, health map, risk, reliability, spares, plan and plan decisions, what-if jobs, evidence, data quality, signed HUMS ingest; `security.py` roles and sessions, `users.py` accounts, `settings.py` configuration; `reports/` printable fleet and aircraft reports |
| `web/` | React + TypeScript dashboard: readiness with forecast, health map, aircraft pages, planner, spares, what if, evidence, reliability, data integration; a short guide on every view |
| `airpower/audit.py` | Append-only, hash-chained audit log |
| `deploy/` | Dockerfile, compose file (app, TLS proxy, PostgreSQL profiles), nginx config, demo accounts |
| `docs/` | Plan, results, `VALIDATION.md` (generated), `SECURITY.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `DATA_PLAN.md`, `FMEA.md`, `ROADMAP.md`, `SIH_TRACEABILITY.md`, `THIRD_PARTY.md` (generated), `DEMO.md` |
| `tools/` | `reconcile.py` (integration vs truth), `fetch_real_data.py`, `build_validation.py` (docs/VALIDATION.md), `licences.py`, `init_deploy.py`, `backup.py`, `clean_install_check.sh`, `phase*_pipeline.sh`, `rebuild_stores.sh` |
| `tests/` | Reused modules, datasets, simulator invariants, integration end to end, health models, reliability, spares and planner, P2 and the twin, API and reports |
