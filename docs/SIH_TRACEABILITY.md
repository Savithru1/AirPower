# AirPower: traceability to the problem statement

Each element of SIH 2026 problem 26249, *Air Power: Predictive Maintenance & Fleet Availability* (Ministry of
Defence, Defence Services Staff College; Software; Transportation & Logistics), mapped to where it is
implemented and the evidence. The elements are those of the plan's reading of the statement
([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) section 0); check them against the statement text before
submission. Numbers are in [VALIDATION.md](VALIDATION.md), generated from the evaluation outputs; they are not
repeated here except where a row needs one.

All fleet-level evidence is on a **simulated fleet** whose sensor signals are real NASA and university data;
each model is also scored on public real data where a counterpart exists ([DATA_PLAN.md](DATA_PLAN.md)). No real
fleet data was available.

Status: **Implemented** = code and a test or a measured result exist; **Partial** = implemented with a stated
limitation; **Not done**.

## 1. The problem

| Element | Where | Evidence | Status |
|---|---|---|---|
| Aircraft availability is low | Operational availability and its split into serviceable, maintenance, awaiting spares and scheduled, per unit and per day (`airpower/twin/status.py`, Readiness view) | VALIDATION s.11: the reactive baseline in the simulated fleet; derived serviceability agrees with truth on 97.8 % of aircraft-days (s.10) | Implemented |
| Maintenance is reactive | Policies P0 (reactive), P1 (preventive), P2 (predictive) in `airpower/fleetsim/` | VALIDATION s.11-13: paired years, 95 % intervals | Implemented |
| Four sources, not integrated: HUMS | HUMS adapter, signed uplink, `health_indicator` store | `tests/test_integration.py` (signed ingest: accepted, replay, forgery, bad unit) | Implemented |
| Four sources: technical records | Records adapter: defects with free text, life cards, inspection history, work done | VALIDATION s.10: 11 of 11 injected inconsistency types handled | Implemented |
| Four sources: spares | Stores adapter: stock, transactions, repair returns, part-number supersessions | As above | Implemented |
| Four sources: maintenance agencies | Agency capacity and work orders; planner capacity constraints | As above; VALIDATION s.9 | Implemented |
| Real formats of the four systems | Prototype reads CSV / JSON exports of the simulator | `registry.yaml` holds the conventions | **Partial**: real adapters are Phase A of the roadmap |

## 2. The technology opportunity

| Element | Where | Evidence | Status |
|---|---|---|---|
| AI / ML predictive maintenance | A1 engine RUL with 90 % intervals; A2 pre-maintenance risk; A3 gearbox faults against the unit's own baseline; A4 battery RUL; A5 fleet risk records (`airpower/health/`); A6-A9 Weibull, defect text to ATA, repeat and rogue detection, KPIs (`airpower/nlp/`, `airpower/reliability/`) | VALIDATION s.1-7, each scored on held-out public data or an unseen simulated run | Implemented (see limits in VALIDATION) |
| IoT / HUMS | Per-sortie HUMS records for engines, gearboxes and batteries, a signed HTTP uplink (HMAC, timestamp, replay cache), trend processing | `tests/test_integration.py`; SECURITY.md 3.2 | Implemented; real ground-station formats not done |
| Digital twin | Aircraft twin: as-maintained configuration, life consumed, component health with intervals, inspections due, recommended actions with reasons (`airpower/twin/views.py`, tail page). Fleet twin: the simulator started from the store's state, run forward under levers (`airpower/twin/whatif.py`) | VALIDATION s.14: forecasts from the records against what happened next, three stores | Implemented |
| Integrated analytics platform | Unified data model, integration layer, API, dashboard, reports | `docs/ARCHITECTURE.md`; 37 API and report routes under deny-by-default rules | Implemented |

## 3. Outcomes the platform is meant to deliver

| Outcome | Where | Evidence | Status |
|---|---|---|---|
| Predict component failures and removals, with uncertainty | Risk records: remaining life with interval, probability of removal within 100 FH, explanation | VALIDATION s.1, 3, 4, 5 | Implemented |
| Turn predictions into a spares plan | Prediction-driven demand, METRIC allocation, AOG list with transfer or expedite advice (`airpower/spares/`) | VALIDATION s.8: availability up 7.1 points with 11 % less stock in 20 paired simulated years | Implemented |
| Turn predictions into a maintenance schedule against agency capacity | CP-SAT planner: inspections and predicted removals, bundling, flying-task requirement, accept / move with a record | VALIDATION s.9: solved to optimality in about 10 s for 48 aircraft x 90 days; fewer aircraft-days down than the due-date rule | Implemented |
| Raise availability, and show it | Policy study with common random numbers, the platform's own models in the loop, value-of-prediction curve | VALIDATION s.12-13 | Implemented; the gain from prediction on top of stock reallocation is smaller and is reported as measured |
| What-if before a decision | Levers: flying rate, exercise, repair and procurement times, crews and bays, stock, policy; difference with a 95 % interval on the same simulated futures | Evidence view; `tests/test_twin.py` | Implemented |
| Different users see what they need | Five roles, views by role, printable fleet and aircraft reports | `tests/test_security.py` | Implemented |
| On-premises and air-gapped | Static dashboard (no CDN), no external calls at run time, container deployment | SECURITY.md 3.5; OPERATIONS.md | **Partial**: container and PostgreSQL profile written and checked, not run |
| Access control and accountability | Roles, deny-by-default routes, hash-chained audit log, signed HUMS uplink, model-file verification | SECURITY.md; 20 tests | Implemented for the prototype; production items in SECURITY.md section 6 |

## 4. Honest limits

* The fleet is simulated; parameters are assumptions, replaceable in YAML. Behaviour on a real fleet is what Phase B of the roadmap measures.
* Prediction adds a smaller gain than spares reallocation, and the value-of-prediction curve shows it shrinks as the models get worse.
* The defect-text model is trained on civil reports; transfer to a unit's logbook language needs fine-tuning.
* Single-echelon spares model; the planner does not allocate sorties; one API process.
* Security: demo keys and accounts, no multi-factor, no PKI, unencrypted SQLite, no penetration test.

## 5. Evidence index

| Evidence | File |
|---|---|
| Model and fleet results | [VALIDATION.md](VALIDATION.md) |
| Plan, results by phase, deviations | [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) sections 15-21 |
| Security and tests | [SECURITY.md](SECURITY.md), `tests/` |
| Failure modes | [FMEA.md](FMEA.md) |
| Datasets and terms | [DATA_PLAN.md](DATA_PLAN.md), [THIRD_PARTY.md](THIRD_PARTY.md) |
| Demo | [DEMO.md](DEMO.md) |
| Reproduce | README.md, [OPERATIONS.md](OPERATIONS.md), `tools/phase*_pipeline.sh` |
