# AirPower: architecture

How the pieces fit, what each owns, and the decisions behind them. The plan
([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)) says why; this says how it is built.

## 1. The flow

```
 four source systems            integration              analytics                 decisions                  people
 (exports, files)                                                                                           
 HUMS downloads ──┐             adapters per source       health models:            spares: demand,           commander
 technical records│  ───────►   entity resolution   ───►  engine RUL, gearbox,  ─► METRIC allocation,   ───►  engineering officer
 stores / spares  │             (tail, part number,       battery, pre-maint        AOG risk                  logistics officer
 agency work orders┘            serial, work order)       risk records            ─ planner (CP-SAT)          analyst
                                data-quality report       records analytics:        inspections + removals    administrator
        signed HUMS uplink ─►   unified store ◄───────    ATA, repeat, rogue,     ─ fleet twin: forecast,
        (HMAC)                  (SQLite / PostgreSQL)     Weibull, KPIs               what-if, policy study
```

One rule keeps the layers apart: **every model writes `risk_record` rows and every decision layer reads
only those**, and the API and dashboard read only the store. A model can be replaced without touching
the planner, and nothing reads the simulator's truth except scoring code.

## 2. Components

| Component | Where | Responsibility |
|---|---|---|
| FleetSim | `airpower/fleetsim/` | SimPy discrete-event simulation of 48 aircraft, two bases and a depot: flying, component ageing, defects, inspections, agencies, two-echelon stores. Real NASA / CWRU / SEU signals replayed as HUMS. Writes the four fragmented source exports and a truth log (scoring only). Also the engine of the policy study and the fleet twin |
| Data model | `airpower/model/` | One schema (SQLAlchemy 2) for SQLite and PostgreSQL / TimescaleDB. Derived tables are rebuilt on each load; **operational** tables (users, audit log, plan decisions) are kept and versioned in `migrate.py` |
| Integration | `airpower/ingest/` | Adapters, entity resolution (tails, part-number aliases and supersessions, serial typos), duplicate and link rules, a status engine that derives serviceability from records, loader, signed HUMS envelope |
| Health models | `airpower/health/` | A1 engine RUL, A2 pre-maintenance risk, A3 gearbox faults, A4 battery RUL, A5 risk records; `artifacts.py` checks model files before loading |
| Records analytics | `airpower/nlp/`, `airpower/reliability/` | A6 Weibull with censoring, A7 defect text to ATA chapter, A8 repeat defects and rogue serials, A9 KPIs |
| Decisions | `airpower/spares/`, `airpower/plan/` | Prediction-driven demand, METRIC allocation, AOG risk; CP-SAT maintenance planner; due-date rule as the baseline |
| Fleet twin | `airpower/twin/` | `views.py` (aircraft twin, health map, recommended actions), `whatif.py` (the simulator started from the store's state, with levers), `study.py` (the policy comparison), `status.py` |
| Policies | `airpower/fleetsim/policies.py`, `predictive.py` | P0 reactive, P1 preventive, P2 predictive (the platform's models running in the loop) |
| API | `server/app.py` | FastAPI: read routes, auth and roles, admin, audit, what-if jobs (`jobs.py`), reports (`reports/`), signed ingest |
| Dashboard | `web/` | React + TypeScript + Vite + ECharts, built to static files that the API serves (no CDN, runs air-gapped) |

## 3. Data model in one page

* **Fleet:** `aircraft_type`, `unit`, `aircraft`, `location`, `agency`, `part`, `part_alias`.
* **Configuration:** `installation` (which serial in which position, hours at export, life limit), `component_change`.
* **Usage and health:** `sortie`, `hums_record`, `health_indicator` (a hypertable on PostgreSQL with TimescaleDB).
* **Maintenance:** `defect` (ATA, text, source of the ATA: recorded or inferred), `work_order`, `work_order_defect`, `inspection`, `inspection_status`.
* **Supply:** `stock_snapshot`, `stock_txn`, `repair_return`.
* **Derived:** `aircraft_status` (serviceable / NMCM / NMCS / scheduled intervals), `risk_record`, `reliability_part`, `rogue_candidate`, `repeat_defect`, `spares_target`, `aog_risk`, `plan_job`, `meta` (reports and summaries as JSON).
* **Lineage and quality:** `ingest_batch`, `dq_issue` (every inconsistency and what was done about it).
* **Operational (kept across loads):** `app_user`, `audit_log`, `plan_decision`, `schema_info`.

Times are naive UTC; the dashboard shows base local time. Every row keeps the identifier it had in its source system.

## 4. Decisions worth knowing

| Decision | Why |
|---|---|
| One interface between models and decisions (`risk_record`) | Models differ in kind (a trend RUL, an anomaly detector, a battery trend); the planner and spares code must not care |
| Per-serial baselines for gearboxes | A fleet-wide vibration model did not transfer across operating conditions (Phase 3); each gearbox is compared with its own healthy start |
| METRIC rather than a per-part service level | Spending the same money where a backorder costs most aircraft raised availability 7 points with 11 % less stock in simulation |
| CP-SAT with a due-date baseline | The planner is judged against the rule it replaces, with the same accounting |
| Policies compared with common random numbers and a warm-up | Differences of a few points are only visible when both arms meet the same random events, and the fleet must start in equilibrium |
| The platform's own models run inside the policy study, trained on separate seeds | A policy that reads the simulator's truth would be rigged; predictors are scored on what they do |
| Value-of-prediction curve | Shows how much of the gain survives worse models, and with none |
| The twin starts from the store, not from the simulator | It is a forecast from records, and it is checked against what the simulated fleet then did (VALIDATION.md section 14) |
| Deny-by-default access rules in one table | One place to review; a test fails if a route has no rule |
| Operational tables apart from derived ones | A data load must never delete who decided what |
| SQLite by default, PostgreSQL by configuration | The prototype runs anywhere; the production path is written but was not run where this was built |

## 5. Runtime view

```
 browser ──HTTPS──► [TLS proxy] ──► API (uvicorn, 1 process) ──► store (SQLite file | PostgreSQL)
                                      │  └─► what-if jobs: thread ─► process pool (SimPy runs)
                                      └─► model files (verified), outputs/ (study results), data/ (replay trajectories)
 HUMS ground station ──signed POST──► API
 integration run (CLI) ──────────────► store            (python -m airpower.ingest.load RUN)
```

Run-time sizes on the build machine: integrating three simulated years takes about 4 minutes; a 90-day plan solves to optimality in about 10 s; a 30-day, 20-future forecast takes about
30 s; the first health map for a date takes about 4 s, then it is cached.

## 6. Limits to keep in mind

* The fleet is synthetic. Parameters are in `airpower/fleetsim/config/*.yaml`, each an assumption. See [DATA_PLAN.md](DATA_PLAN.md).
* One API process, one writer for SQLite. PostgreSQL and more processes need the audit lock and the what-if job table to move into the database.
* Spares model: single echelon with pooled stock (VARI-METRIC is the production version).
* The planner keeps each aircraft's flying rate fixed; it plans maintenance, not sortie allocation.
