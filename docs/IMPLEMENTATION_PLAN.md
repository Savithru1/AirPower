# AirPower: implementation plan

SIH 2026 problem statement **26249**, *Air Power: Predictive Maintenance & Fleet Availability*
(Ministry of Defence, Defence Services Staff College; category Software; theme Transportation &
Logistics). Plan v1, 2026-10-05.

---

## 0. The problem, stated as something we can measure

The statement says aircraft availability is low because maintenance is **reactive** and the data
that could make it predictive is spread across four systems that do not talk to each other:

| Source in the statement | What it holds in practice | Why it hurts availability when isolated |
|---|---|---|
| Aircraft health-monitoring systems (HUMS / EHM) | Post-flight downloads: engine and gearbox indicators, vibration, exceedances | Degradation is visible but nobody turns it into a dated removal forecast |
| Technical records | Servicing records (Form-700-style), defect entries (free text), component life cards (hours / cycles / calendar), work done | Repeat defects and rogue serials go unnoticed; life-limited parts come due in clusters |
| Spares | Stock per base and depot, dues-in, repair pipeline, lead times, part-number supersessions | Parts are ordered after the failure, so the aircraft waits (NMCS: not mission capable, supply) |
| Maintenance agencies | Squadron (O-level), station / wing (I-level), BRD / OEM (D-level) capacity and queues | Inductions are not planned against capacity, so the aircraft waits again (NMCM: not mission capable, maintenance) |

**The measurable goal:** raise operational availability
`Ao = MTBM / (MTBM + MDT)`, where mean down time `MDT = repair time + logistics delay + admin
delay`. Prediction lowers unscheduled MTBM losses; integration with spares and agencies lowers
the delay terms, which in most fleets are larger than the repair time itself.

**What we build:** an integrated maintenance analytics platform that (1) joins the four sources
into one model of the fleet, (2) predicts component failures and removals with uncertainty,
(3) turns those predictions into a spares plan and a maintenance schedule that maximise aircraft
available for the flying task, and (4) proves the gain in a fleet-level digital twin by comparing
reactive, preventive and predictive policies.

**Honest framing (same rule as AeroTwin).** No real military fleet data is available to us. Every
model is trained and scored on **public real or NASA-simulated datasets** where a counterpart
exists, and the fleet-level demonstration runs on a **synthetic fleet** whose parameters are
documented and configurable. We say which number comes from which.

---

## 1. Relationship to AeroTwin

AeroTwin (`F:\AeroTwin\sih26054_v2`, SIH26054) is an **engine-level** digital twin: one engine
type, per-second residuals, diagnosis and RUL. AirPower is the **fleet-level** layer above that
kind of system: many aircraft and component types, technical records, spares, agencies and
scheduling. About a third of AirPower's analytics and most of its backend plumbing can be taken
from AeroTwin (section 9). AirPower must also stand on its own for the judges, so AeroTwin
appears only as an optional plug-in health source for a UAV type (section 9, last row).

---

## 2. Solution overview

```
 FRAGMENTED SOURCES                 INTEGRATION              ANALYTICS                     DECISIONS                    USERS
 (FleetSim exports them,            (airpower/ingest)        (airpower/health,             (airpower/plan,
  deliberately messy)                                         reliability, nlp)             spares, twin)

 HUMS post-flight files  ─┐                               ┌─ Component RUL + interval ─┐  ┌─ Spares: predictive demand,─┐  Commander:
 Technical records (CSV,  ├─► adapters ─► entity     ─►   ├─ Pre-maintenance risk      ├─►│  METRIC stock allocation,   │  readiness, forecast,
   free-text defects)     │   resolution  unified         ├─ Vibration fault class     │  │  transfers, AOG alerts      │  what-if
 Spares / stores exports  │   + data-     fleet store     ├─ Defect text -> ATA,       │  ├─ Planner (CP-SAT): when    ─┤  Engineering officer:
 Agency capacity / WOs   ─┘   quality      (Postgres +    │  repeat / rogue detection  │  │  and where to induct, what  │  tail health, plan
                              report       Timescale)     └─ Weibull / survival hazard ┘  │  to bundle, who flies      │  Logistics officer:
                                                                                          └─ Fleet twin: forecast and ─┘  spares risk
                                                                                             policy comparison
```

The loop closes in the fleet twin: today's integrated state seeds a Monte Carlo simulation of the
next weeks under the recommended plan, which gives an availability forecast with a confidence band
and lets a commander ask what-if questions.

---

## 3. Repository layout (proposed)

```
F:\AirPower\
  airpower/
    model/         unified data model (SQLAlchemy + pydantic), aligned with ASD S5000F in-service
                   feedback concepts: Aircraft, Type, ComponentPosition, Serial, Installation,
                   Sortie, HealthIndicator, Defect, WorkOrder, Inspection, SKU, Stock, Order,
                   Agency, CapacitySlot
    fleetsim/      discrete-event fleet simulator (SimPy): flying programme, component ageing and
                   failure, HUMS signal emission, inspections, agencies, stores and repair
                   pipeline, policy plug-ins; writes the four source exports
    ingest/        one adapter per source, entity resolution (tails, part numbers, serials),
                   data-quality report, loaders
    health/        HUMS analytics: anomaly detection, engine RUL, vibration fault classes,
                   battery RUL, pre-maintenance risk (NGAFID), uncertainty (conformal / PF)
    reliability/   Weibull fits, survival models with HUMS covariates, MTBF / MTBUR / NFF,
                   rogue-serial detection
    nlp/           defect text -> ATA chapter, repeat-defect clustering, records search
    spares/        demand forecasting (Croston / SBA baseline vs prediction-driven),
                   METRIC-style stock allocation, AOG risk
    plan/          CP-SAT maintenance scheduler, opportunistic bundling, flying-hour balancing
    twin/          aircraft twin (as-maintained configuration, life consumed, health) and fleet
                   twin (forecast and what-if on top of fleetsim)
  server/          FastAPI backend, RBAC, audit log, reports, WebSocket
  web/             React + TypeScript + Vite dashboard, ECharts; built to static files
  data/            manifest.json, raw/, processed/ (large AeroTwin sets referenced, not copied)
  tools/           fetch, calibration, validation and report generators
  tests/
  deploy/          Dockerfiles, docker-compose (API + Postgres/TimescaleDB + web)
  docs/            this plan, ARCHITECTURE, DATA_PLAN, VALIDATION, SECURITY, ROADMAP,
                   SIH_TRACEABILITY
```

---

## 4. Data strategy

Two layers, kept separate:

### 4.1 Real-data evidence (each model proven on data we did not generate)

| Asset | Dataset | Status | Used for |
|---|---|---|---|
| Turbofan engine | NASA C-MAPSS FD001-FD004 (run-to-failure, 21 sensors, 3 operating settings) | **Already downloaded** in AeroTwin `data/raw/cmapss` | Engine RUL model and its intervals |
| Turbofan engine (stretch) | NASA N-CMAPSS (real flight profiles) | To fetch | Harder RUL benchmark |
| Piston engine, whole aircraft | NGAFID (CC-BY 4.0): 11,446 real flights, 1 Hz, flights in the 2 days before vs after **unplanned maintenance**, 19 issue labels, 5 folds | **Already downloaded and harmonised** in AeroTwin (`data/processed/ngafid`, 5.7 GB) | Real-data proof that HUMS-type signals predict an upcoming maintenance action |
| Bearings, gearbox | CWRU bearing, SEU gearbox | **Already downloaded and prepared** in AeroTwin | Gearbox / bearing HUMS fault classes for the helicopter type |
| Battery | NASA battery ageing | **Already downloaded and prepared** in AeroTwin | Battery capacity-fade RUL |
| Defect text | FAA Service Difficulty Reporting System (free text with JASC / ATA-based codes); MaintNet aviation logbook text | To fetch; licences to verify | Defect -> ATA classifier, repeat-defect detection, records search |
| Spares demand | No public military set. Synthetic intermittent demand; public intermittent-demand benchmarks only if a licensed one is found | Synthetic | Forecast and stock-policy benchmarks |

Large AeroTwin data stays where it is; AirPower reads it through `data/manifest.json` with an
`AEROTWIN_DATA` path. Only small sets (C-MAPSS, 56 MB) are copied.

### 4.2 Synthetic fleet (FleetSim) for the integrated demonstration

* **Fleet (configurable):** Type J, a generic single-engine jet (2 squadrons x 18 aircraft), and
  Type H, a generic twin-engine helicopter (1 unit x 12). Two operating bases with O / I-level
  maintenance, one depot (D-level, repairs rotables), central stores. Generic types only; no claim
  to represent a specific in-service aircraft.
* **Component tree per type:** 25-40 tracked positions, each with a maintenance policy
  (hard-time / on-condition / condition-monitored), Weibull shape and scale, repair time, SKU,
  rotable or consumable, depot turnaround, procurement lead time, unit cost. All in YAML.
* **Real signals inside the synthetic fleet:** condition-monitored engines replay C-MAPSS
  trajectories mapped to engine serials; helicopter gearboxes emit features from CWRU / SEU
  segments for their degradation state; batteries follow NASA fade curves. The models in the loop
  therefore see realistic signals, not signals invented by the same code that judges them.
* **Flying programme:** daily task per squadron (sorties, hours) with surge periods.
* **Deliberate fragmentation:** each source is written in its own format and with its own faults:
  tail-number formats that differ by system, superseded and aliased part numbers, missing and
  duplicated records, late entries, abbreviations and typos in defect text, inventory snapshots on
  a different cadence. The integration layer has to fix these, and the data-quality report shows
  what it fixed. That is the "not adequately integrated" part of the statement, made visible.

---

## 5. Analytics: models, data, metrics

| # | Capability | Method | Trained / scored on | Metric and initial target |
|---|---|---|---|---|
| A1 | Engine RUL with interval | Gradient boosting on windowed features (baseline), optional 1D-CNN / LSTM; split-conformal intervals; ProgPy particle filter from AeroTwin for the online update | C-MAPSS | RMSE and NASA score per subset; FD001 RMSE <= 16 cycles (GBM), <= 13 with a CNN; 90 % interval empirical coverage 87-93 % |
| A2 | Pre-maintenance risk ("this aircraft needs maintenance soon") | Per-flight features + gradient boosting; AeroTwin `Diagnoser` (random forest + SHAP + IsolationForest) for the issue class | NGAFID, the dataset's own 5 folds | AUC, accuracy, per-issue F1; compared with the published NGAFID baselines on the same folds |
| A3 | Gearbox / bearing fault class | AeroTwin edge DSP features + classifier | CWRU, SEU | Accuracy on held-out loads / recordings (split by recording, never by window, to avoid the known CWRU leakage) |
| A4 | Battery RUL | Capacity-fade fit + PF | NASA battery, leave-one-cell-out | Alpha-lambda, RMSE at end of life |
| A5 | HUMS anomaly detection | AeroTwin EWMA z-scores, learned thresholds with cross-validated margin, persistence | FleetSim HUMS + NGAFID healthy flights | False alarms per 100 flight hours, detection delay |
| A6 | Failure hazard per position | Weibull fit per part type; Cox / Weibull-AFT with HUMS covariates where monitored | FleetSim records (generated with known truth, so the fit can be checked) | Parameter recovery error, concordance index |
| A7 | Defect text -> ATA chapter | TF-IDF + linear model baseline; sentence embeddings if PyTorch is approved | FAA SDRS / MaintNet | Top-1 accuracy and macro-F1 at chapter level |
| A8 | Repeat defects, rogue serials | Rules (same tail, same ATA, within N sorties) + clustering of embeddings; rogue = serial with removal rate far above its part number's | FleetSim (rogues injected) + SDRS | Precision / recall on injected rogues |
| A9 | Reliability KPIs | MTBF, MTBUR, NFF rate, unscheduled removal rate per part number, per base | Integrated store | Shown on dashboard |

All model outputs land in one place: a **risk record per (aircraft, position)** with a removal-time
distribution, a confidence and an explanation. Everything downstream consumes only that record.

---

## 6. Decision layer

### 6.1 Spares (`airpower/spares`)

* **Prediction-driven demand.** Expected removals per SKU over the lead time = sum over the fleet
  of each position's removal probability in that window (from section 5), plus the scheduled
  removals from life limits. Compared against Croston / SBA forecasts that only see past demand.
* **Stock allocation.** METRIC-style marginal analysis (Sherbrooke's model, built for USAF
  recoverable spares): allocate a budget across SKUs and bases to minimise expected backorders,
  which is the direct driver of NMCS aircraft. Output: the **cost vs availability curve**, the
  chart that tells a commander what an extra amount of spares money buys in aircraft available.
* **Actions:** reorder recommendations, inter-base transfers, AOG risk alerts ("Tail J-14 likely
  needs part P within 12 days; lead time 30 days; zero serviceable stock").

### 6.2 Maintenance planning (`airpower/plan`, OR-Tools CP-SAT)

Rolling 60-90 day horizon, daily buckets, re-planned whenever new data arrives.

* **Decisions:** which aircraft fly on which day, when and at which agency each aircraft is
  inducted, which work is bundled into each induction.
* **Constraints:** daily flying task per squadron, life limits and inspection intervals (hours /
  cycles / calendar), agency capacity and skills (cumulative constraint on interval variables),
  spares availability and dues-in, aircraft already unserviceable.
* **Objective:** maximise aircraft-days available and flying task met; minimise risk-weighted
  exposure (flying an aircraft whose removal probability is high); penalise inductions.
* **Opportunistic bundling:** when an aircraft is down anyway, pull forward high-risk or
  near-limit items so it is not inducted twice.
* **Flying-hour balancing (phase flow):** stagger hours-to-next-inspection across the fleet so
  aircraft do not come due together and empty the line.
* **Human in the loop:** the planner recommends, the engineering officer accepts or overrides,
  every override is logged with a reason.

---

## 7. Fleet digital twin and the headline result

* **Aircraft twin:** as-maintained configuration (which serial is in which position), life
  consumed per position, latest health indicators, risk records, open and deferred defects,
  inspections due.
* **Fleet twin:** FleetSim seeded from the current integrated state, run forward N weeks under the
  current plan, many replications, giving availability with a confidence band. What-if levers:
  flying rate, spares budget, lead times, agency capacity, policy.

**Policy comparison (the result we present):** same fleet, same random seeds (common random
numbers), at least 30 replications of one year each:

| Policy | Description |
|---|---|
| P0 Reactive | Repair on failure; hard-time items replaced at limit |
| P1 Preventive | Fixed intervals for everything |
| P2 Predictive (ours) | Risk-driven inductions, bundling, prediction-driven spares, CP-SAT rolling plan |

Reported: mean availability and flying task met (with 95 % CI), NMCS and NMCM aircraft-days,
unscheduled removals, no-fault-found removals, spares spend, labour hours.

**Guarding against a rigged result** (AeroTwin's same-source-bias lesson):

1. Only some positions are condition-monitored; the rest are invisible to the predictor.
2. Sensors are noisy and have gaps; predictors in the loop are trained on separate seeds or on
   the real datasets, never on the evaluation runs.
3. False alarms cost something: an unnecessary removal consumes a spare, labour and a no-fault-
   found repair at the depot.
4. **Value-of-prediction curve:** degrade the predictors on purpose (RMSE x1.5, x2, AUC down to
   0.6) and plot how much of the availability gain survives. This shows the gain is not an
   artefact of perfect foresight and tells a buyer how good the models must be.

---

## 8. Dashboard (`web/`) by role

DSSC is a staff college, so the evaluators are likely to be serving officers. The first screen
answers command questions; engineering detail is one click deeper.

| View | For | Content |
|---|---|---|
| Readiness | Commander | Serviceable now vs task per squadron; 30-day availability forecast with band; top NMCS / NMCM drivers; KPIs |
| Fleet health map | Engineering officer | Tails x systems risk heatmap; drill into any cell |
| Tail page | Engineering officer | Twin: configuration, life-limited parts, RUL distributions, HUMS trends, defect history (ATA-tagged), recommended actions with explanation |
| Planner | Engineering officer | Gantt by agency, recommended inductions and bundles, accept / override |
| Spares | Logistics officer | Predicted demand vs stock, AOG risks, reorder and transfer recommendations, cost vs availability curve |
| Records | All | Search over defects, repeat-defect and rogue-serial reports, reliability KPIs |
| What-if | Commander, planners | Sliders for flying rate, budget, lead time, capacity, policy -> forecast |
| Data integration | Analyst | Source freshness, entity-resolution statistics, data-quality issues |
| Reports | All | PDF / HTML fleet and tail reports (AeroTwin's renderer) |

---

## 9. What we take from AeroTwin

Code is copied with a header noting its origin, then adapted. Nothing is imported across
projects at run time.

| AeroTwin file | AirPower destination | Change needed |
|---|---|---|
| `prognostics/rul.py` (ProgPy model, log-domain SIR particle filter, MonteCarlo RUL, alpha-lambda) | `airpower/health/rul_pf.py` | Generalise from the UKF health parameter to any health index (C-MAPSS HI, battery capacity) |
| `twin/detect.py` (EWMA, learned thresholds, persistence, CV margin) | `airpower/health/detect.py` | Channel names come from component configs instead of contract v2 |
| `twin/diagnose.py` (`Diagnoser`: RF + SHAP + IsolationForest, "unknown" below confidence) | `airpower/health/diagnose.py` | New feature tables (NGAFID, vibration); drop engine-specific `localise` |
| `edge/dsp.py`, `data_prep/vibration.py` | `airpower/health/vibration.py`, `data_prep/` | Reuse as is for gearbox / bearing features |
| `data_prep/battery.py`, `tools/fit_battery.py` | `airpower/health/battery.py` | Reuse |
| `data_prep/ngafid.py` (+ processed data) | `data_prep/ngafid.py` | Add per-flight feature extraction for A2; read processed data via `AEROTWIN_DATA` |
| `data/raw/cmapss` | `data/raw/cmapss` (copy, 56 MB) | Add prep module |
| `tools/fetch_real_data.py`, `data/manifest.json` | same | New entries: SDRS, MaintNet, N-CMAPSS |
| `tools/realism_gate.py` (sim-vs-real classifier AUC) | `tools/realism_gate.py` | Apply to FleetSim HUMS vs C-MAPSS / NGAFID |
| `edge/link.py` `SignedJson`, `server/app.py` `ingest_sealed`, `server/zmq_ingest.py` | `airpower/ingest/secure.py`, `server/` | HMAC-signed HUMS uploads and optional live feed |
| `server/app.py`, `server/db.py`, `server/config.py` (app factory, DI, columnar JSON) | `server/` | New schema and routes; SQLAlchemy instead of raw sqlite3 |
| `server/report.py`, `server/render.py`, `server/templates/report.html` | `server/reports/` | Engine status rules -> aircraft status (serviceable / with limitations / unserviceable) |
| `server/schema_timescale.sql` | `server/schema/` | Hypertable pattern for health indicators |
| `deploy/` (Dockerfiles, compose with TimescaleDB), `.github/workflows/ci.yml` | `deploy/`, `.github/` | Add web build stage |
| `ui/vendor/three.min.js`, `OrbitControls.js` | `web/` (optional) | 3D aircraft with positions coloured by risk on the tail page |
| `twin/advisory.py` | `airpower/health/advisory.py` | Advisory text per finding |
| `docs/` set (ARCHITECTURE, DATA_PLAN, FMEA, SECURITY, ROADMAP, SIH_TRACEABILITY, VALIDATION) | `docs/` | Same structure, new content |
| AeroTwin as a system (`/api/missions/{name}/alerts`, `/rul`) | `airpower/ingest/aerotwin.py` (optional) | A MALE UAV type whose engine health comes from AeroTwin |

**Not reused:** `sim/` (JSBSim flight and engine plant, too detailed for fleet level), `contract/`
(CAN), the onboard parts of `edge/` (gateway, health frame, MAVLink), `twin/ukf.py`,
`twin/kan.py`.

---

## 10. Phases

Assumes a 6-person team and roughly 9 weeks to a December finale; adjust once the dates are
confirmed. Phases 3, 4 and 7 run in parallel with the others.

| Phase | Weeks | Output | Done when |
|---|---|---|---|
| **0. Setup** | 0.5 | Git repo, skeleton, venv, copied AeroTwin modules with tests passing, manifest, CI | `pytest` green; C-MAPSS and NGAFID load from the new repo |
| **1. FleetSim v1 + data model** | 1-2 | YAML fleet config, SimPy simulator with P0 / P1 policies, four messy source exports, unified schema | One simulated year in under 2 min; baseline availability numbers for P0 and P1 |
| **2. Integration layer** | 2 | Adapters, entity resolution, data-quality report, loader into Postgres / Timescale, first API routes | Every injected inconsistency is resolved or reported; counts reconcile with FleetSim truth |
| **Demo 1 (end of week 2)** | | Thin dashboard: readiness view + data-integration view on integrated data | End-to-end slice works |
| **3. Health models on real data** | 2-4 | A1-A5 trained and scored; risk-record interface | Section 5 metrics recorded in `docs/VALIDATION.md` |
| **4. Records and reliability** | 3-4 | A6-A9: ATA classifier, repeat / rogue detection, Weibull and survival fits, KPIs | Metrics recorded; rogues found in FleetSim |
| **5. Decision layer** | 4-6 | Prediction-driven spares, METRIC allocation, CP-SAT planner with bundling and phase flow | 48 aircraft x 90 days solved in under 60 s; cost vs availability curve |
| **6. Fleet twin + policy study** | 6-7 | P2 policy in FleetSim, 30+ replications, value-of-prediction curve, what-if API | Section 7 table with confidence intervals, whatever the result |
| **7. Dashboard** | 3-8 | All section 8 views on the live API | Usable by someone who has not seen it, in a 10-minute demo |
| **8. Hardening + submission** | 8-9 | RBAC, audit log, Docker deploy, docs, demo script, deck | Clean install from the README on a fresh machine |

**Team split (one owner each, pairs at the joins):** (1) FleetSim + data model, (2) integration +
backend, (3) health / prognostics models, (4) NLP + reliability, (5) spares + planner
optimisation, (6) dashboard.

---

## 11. Technology

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.13 (matches the AeroTwin venv) | All reused code is Python |
| Simulation | SimPy | Standard discrete-event library, simple process model |
| Optimisation | OR-Tools CP-SAT | Interval and cumulative constraints fit scheduling directly; free |
| ML | scikit-learn, LightGBM, lifelines, ProgPy, SHAP | Already proven in AeroTwin; CPU only |
| Deep learning | PyTorch, **only if approved** (~2.5 GB install) | Needed only for the CNN / LSTM RUL model and text embeddings; every capability has a non-DL baseline first |
| Store | PostgreSQL + TimescaleDB in Docker (Docker 29 is installed); SQLite for unit tests; Parquet for bulk telemetry and ML sets | AeroTwin could not run Timescale in its environment; this machine can |
| Backend | FastAPI, SQLAlchemy 2, Alembic | AeroTwin pattern |
| Frontend | React + TypeScript + Vite, ECharts, a data grid | Eight views with Gantt, heatmaps and tables outgrow AeroTwin's single vanilla-JS file; builds to static files that run air-gapped |
| Reports | Jinja2 + reportlab | AeroTwin renderer |

---

## 12. Security and deployment

* On-premises and air-gapped: no CDN, no external calls at run time, all assets vendored.
* Role-based access (commander, engineering officer, logistics officer, analyst, admin) and an
  append-only audit log of logins, plan accepts / overrides and data loads. AeroTwin lists both as
  not done; here they are required.
* HMAC-signed ingest with timestamp window and replay cache (AeroTwin code); TLS between services;
  encryption at rest via the database.
* Data classification label on every record; export only through reports.
* `docs/SECURITY.md` with a STRIDE model, as in AeroTwin.
* Roadmap (`docs/ROADMAP.md`): adapters for the real record, stores and HUMS ground-station
  formats (interfaces to be confirmed with the user agency), pilot on one squadron's historical
  data, then live.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| No real fleet data | Real-data evidence per model (section 4.1); synthetic fleet parameters documented and swappable; say so plainly |
| Simulation proves what it assumes | Section 7 safeguards and the value-of-prediction curve |
| Scope is large | Vertical slice by week 2; every capability has a simple baseline before a clever version |
| Text data licences unclear | Verify before use; FleetSim template text as fallback |
| Planner too slow at fleet size | Daily buckets, rolling horizon, time limit with best solution so far |
| Heavy installs | Ask before installing PyTorch; non-DL baselines first |

---

## 14. Decisions needed before Phase 0

1. **Deadline and stage:** is the next milestone the idea-submission deck or the finale build?
   This decides whether Phase 0 is preceded by a deck.
2. **Team:** six people with the split in section 10, or fewer?
3. **Fleet:** generic jet + helicopter as proposed; add the AeroTwin MALE UAV type or not?
4. **Frontend:** React + Vite as recommended, or stay with vanilla JS like AeroTwin?
5. **PyTorch:** approve the install now or defer until the baselines are done?

---

## 15. Results of Phases 0-2 (2026-10-05)

Decisions taken (the plan's recommendations): finale build, six-person split, generic jet +
helicopter (AeroTwin UAV plug-in deferred), React + Vite dashboard, PyTorch deferred.

### Phase 0: setup

* Git repository, Python 3.13 venv, CI workflow (`.github/workflows/ci.yml`).
* Copied from AeroTwin with an origin note: `rul_pf.py` (ProgPy particle filter), `vibration.py`
  (edge DSP), `secure.py` (HMAC envelope); generalised: `detect.py` (channel list as argument,
  leave-one-group-out margin selection), `diagnose.py` (classifier + IsolationForest + SHAP without
  engine-specific rules). progpy 1.7.1 declares Python < 3.13 but is pure Python and is installed
  with `--ignore-requires-python`, as in AeroTwin's venv.
* `data/manifest.json`: C-MAPSS copied (56 MB); NGAFID, CWRU / SEU and NASA battery read in place
  from AeroTwin; FAA SDRS, MaintNet and N-CMAPSS listed as `to_verify` / `to_fetch`.
* C-MAPSS FD001 / FD003 run-to-failure units split once (seed 26249): 60 per subset may train models,
  40 are reserved for FleetSim replay, so the fleet study never tests a model on its training data.

### Phase 1: FleetSim and data model

* 48 aircraft (2 x 18 jets, 12 helicopters), 24-25 tracked positions per type, 45 part numbers
  (4 shared between types), O / I / D agencies, two-echelon spares with depot repair. A simulated
  year with all exports takes about 6 s (target: under 2 min).
* Calibration was limited to removing modelling artefacts, not tuning to a target:
  * the depot shop capacity was below the repair inflow, so the queue grew without bound;
  * initial component ages are drawn from the renewal-equilibrium distribution, and units inside
    their P-F window on day 1 are "screened" by the inspections they would already have had.
    Without this, day 1 holds too many detectable defects and the first months show a burst of
    findings;
  * stock is sized per policy as the Poisson quantile of pipeline demand at 85 % (rotables) and
    97 % (consumables), using each policy's own removal rate.
* **Baseline policy study** (20 paired one-year replications after a one-year warm-up;
  `outputs/fleetsim/compare_P0_P1_365d_20r.json`):

| | P0 reactive | P1 preventive |
|---|---|---|
| Availability (time-weighted) | 75.5 % +- 1.1 | 81.8 % +- 0.8 |
| Awaiting spares (NMCS) / maintenance (NMCM) / scheduled | 12.4 / 3.6 / 8.5 % | 5.8 / 3.5 / 8.9 % |
| Removals / in-service failures per year | 808 / 294 | 1,294 / 136 |
| Material cost per year (repairs + procurement) | INR 59.7 crore | INR 68.0 crore (+14 %) |

  Paired difference P1 - P0: **+6.3 +- 1.3 points**. P1 also holds more initial stock (sized for
  its 60 % higher removal rate), which this cost line does not include. The helicopter unit is the
  weak spot (long-turnaround rotor and gearbox spares with stocks of 1-3); this is the pattern
  Phase 5's spares optimisation and Phase 6's predictive policy must beat.
* An earlier comparison without warm-up and with stock sized for P0 showed P1 at 13.6 %
  availability. That result reflected the comparison setup, not the policy, and was discarded.

### Phase 2: integration layer

One-year run, seed 1 (`tools/reconcile.py`, `outputs/fleetsim/p0_s1_365d/reconciliation.json`):

| Injected inconsistency | Injected | Handled | How |
|---|---|---|---|
| Defect entered twice | 68 | 68 | merged (1 genuine near-identical snag also merged: precision 68 of 69) |
| Defect without ATA chapter | 331 | 331 | inferred from the part changed, else a text classifier |
| Late entry (> 24 h) | 359 | 359 | reported |
| Work order lost its defect link | 232 | 232 | recovered from the records side |
| Serial number typo | 74 | 72 | normalised, fuzzy-matched, or corrected by the serial stores moved on the same work order |
| P/N format variant / supersession | 435 / 3 | 435 / 3 | alias table |
| Stock number without P/N | 1 | 1 | inferred from serial-number votes |
| Stores demand without aircraft | 44 | 44 | recovered through the work order |
| HUMS duplicate upload / missing download | 115 / 317 | 115 / 317 | merged / reported |

* No correct serial was changed by a correction (checked against truth for all 847 changes).
* HUMS downloads linked to the right sortie across systems (UTC vs local, no tail number in HUMS):
  10,164 of 10,164.
* Serviceability derived from the integrated records alone agrees with the truth for **97.9 %** of
  aircraft-days at 08:00; availability 78.1 % derived vs 78.8 % true.
* API (`server/app.py`) and dashboard (`web/`): readiness by unit, 12-month trend, parts holding
  aircraft, aircraft board with timeline / open defects / HUMS trend / fitted configuration, and the
  data-integration report with sample records. Signed HUMS ingest is tested for replay, forgery and
  unknown units.

### Deviations and known limitations

* **Store:** SQLite is the default because the Docker daemon was not running. The schema and loader
  support PostgreSQL / TimescaleDB (`AIRPOWER_DB`), but that path is not yet exercised.
* **Defect text** is template-based, so the 100 % ATA inference here is not evidence for real text.
  Phase 4 measures it on FAA SDRS / MaintNet.
* **Stores dates** are day-resolution, so NMCS periods derived from records are approximate to a day.
* The loader does a full refresh per run; incremental batches are Phase 8.

---

## 16. Results of Phase 3 (2026-10-05)

Full numbers: [docs/VALIDATION.md](VALIDATION.md) (generated by `tools/build_validation.py`).
Reproduce: `bash tools/phase3_pipeline.sh` (about 20 minutes).

| Capability | Data | Result | Plan target |
|---|---|---|---|
| A1 Engine RUL | C-MAPSS official test sets | RMSE 15.2 / 25.6 / 16.1 / 25.9 cycles (FD001-4), 90 % conformal intervals covering 87-94 % | FD001 <= 16 met; <= 13 needs a CNN (PyTorch deferred) |
| A2 Pre-maintenance risk | NGAFID, 11,446 real flights, dataset folds | 71.1 % accuracy, AUC 0.785; every issue family recognisable before maintenance (AUC 0.67-0.85) | Below the published 76.0 % (ConvMHSA, a sequence model) |
| A3 Gearbox / bearing faults | CWRU, SEU, leave one operating condition out | Detection AUC 1.00 / 0.99 against a healthy baseline (0.96 / 0.71 without); gear fault type 42 % | Detection met; gear fault type advisory only |
| A4 Battery RUL | NASA Ames, leave one cell out | Trend: 26 % within +-20 % of true RUL (AeroTwin best 19 %), 90 % interval coverage 81 % | Indicative only (nine noisy cells) |
| A5 Fleet risk records | FleetSim seed 3, three years, unseen; gearbox detector fitted on seed 2 | Removal due within 100 FH flagged: engines 81 %, batteries 87 %; gearboxes flagged while degrading 53 % at 0.2 % false flags | - |

Design consequences carried forward:
* **Per-serial baselines.** The gearbox result (A3) is why fleet vibration monitoring compares each
  gearbox with its own healthy history rather than with a fleet-wide model.
* **Records improve the models.** Engine age comes from the integrated technical records (life card
  hours minus hours flown since). The engine model was retrained with the age masked on half the
  rows, so it also handles engines whose age the records do not give.
* **One interface downstream.** Every model writes `risk_record` rows. The API (`/api/risk`) and
  dashboard read only these, and the Phase 5 planner will too.

Fixed along the way:
* FleetSim added relative noise to replayed C-MAPSS values, which swamped low-variance channels
  (fan speed); the replay is now exact.
* The risk build took over 13 minutes because explanations made one model call per row; it now takes
  2 minutes for three years.
* The first A5 metric counted lives that reached failure; almost none do, because inspections remove
  them first. The evaluation now scores flags while degrading and against the 100 FH horizon.

Known limitations (open):
* **Engine RUL in the fleet** is about twice the offline error: RMSE 116 FH in range, 65 % interval
  coverage, optimistic by 75 FH when the age is unknown. Engines enter the data mid-life, so trend
  windows are short. Training and calibrating on histories that start mid-life must happen before
  Phase 6 relies on these intervals. *(Done in Phase 6: coverage 80 %, RMSE 108 FH; section 19.)*
* **NGAFID:** the leading feature (oil-pressure variability) may reflect sensor work done during
  maintenance rather than engine condition. Flagged for review.
* **Battery intervals** are wide (x3.9 either side), reflecting the weak A4 model.
* **Loading a three-year run** takes about 4 minutes. The integration pipeline was not optimised in
  this phase.

---

## 17. Results of Phase 4 (2026-10-05)

Full numbers: [docs/VALIDATION.md](VALIDATION.md) sections 6-7. Reproduce: `bash tools/phase4_pipeline.sh`.

**Data.** FAA Service Difficulty Reports 2020-2025 (376,311 real reports, each with a JASC code,
free-text discrepancy, part name / number / serial and registration). Downloaded from the FAA page.
They are US Government data with no restrictions stated, and they are not redistributed.
MaintNet was not needed and stays unused.

| Capability | Data | Result |
|---|---|---|
| A7 Defect text -> ATA chapter | 242k training / 134k later test reports, 39 chapters | Top-1 90.3 %, top-3 97.7 %, macro-F1 0.736 with manual references masked (unchanged from the full text, although 46 % of reports cite their own chapter); 98.2 % on the 60 % most confident; 66 % on FleetSim's template logbook text |
| A6 Weibull per part | FleetSim 3-year run, integrated records | Median fitted / true scale 0.87, shape 0.87. Well identified for parts with 50+ failures; poorly identified for random-failure parts with few failures |
| A8 Rogue serials | same | Precision 56 %, recall 19 % of rogues removed at least twice |
| A8 Repeat defects | same | Precision 60 %, recall 81 % (same aircraft, ATA, similar text within 15 days) |
| A8 on real reports | FAA SDRs | 1,063 serials reported on two or more aircraft; 13.7 % of reports repeat the same aircraft and JASC within 1-15 days |
| A9 KPIs | integrated records | Per part: MTBUR, MTBF, NFF rate, Weibull pattern. Fleet: 3.6 FH between defects, 15 FH between unscheduled removals, 3.2 days mean wait for a part |

**Dashboard.** New Reliability tab:
* fleet KPIs and the per-part table;
* the Weibull failure pattern in plain words ("wear-out: replace before failure helps" or "random:
  preventive replacement wastes spares"), which ties A6 to the P1 policy discussion and to Phase 5;
* repeat defects, rogue candidates and a records search.

**Fixed along the way.**
* *Cohort bias:* the first Weibull fits were 2.5x too long-lived, because units fitted before the data
  started kept their survivors (known age) and lost their failures (unknown age). Fits now use units
  installed inside the window.
* *No-fault-found linkage:* only 65 % of NFF removals were recognised. A component change is logged when
  the replacement is fitted, but the removed unit reaches the shop when the spare is demanded, so the
  shop visit is now searched from the defect date.
* *Placeholder serials:* in the FAA data ("NOT REPORTED" on 179 aircraft) these are excluded, and same-day
  multiple reports from one inspection are not counted as repeats.
* *Dashboard:* tab switching raced the app's own URL updates; a count column (`installed`) was parsed
  as a date.

**Known limitations (open).**
* Rogue detection has low recall over three years: a rogue unit here fails about twice, and two short
  lives are weak evidence.
* Repeat-defect precision is limited by unrelated look-alike snags.
* The SDR classifier is trained on civil reports. Transfer to an operator's own logbook language needs
  fine-tuning on its labelled records. The integration pipeline already trains on the operator's own
  defects for missing ATA codes.

---

## 18. Results of Phase 5 (2026-10-05)

Full numbers: [docs/VALIDATION.md](VALIDATION.md) sections 8-9. Reproduce: `bash tools/rebuild_stores.sh`, then
`bash tools/phase5_pipeline.sh`. New dependency: OR-Tools 9.15 (CP-SAT).

**Spares**

* **The headline result.** Allocating the spares budget with METRIC raises fleet availability by
  **7.1 +- 1.0 points** (75.5 % -> 82.5 %) with **11 % less** inventory value. This was checked in 20
  paired years of fresh simulated fleets, with stock levels computed from a separate training fleet's
  records. At 75 % of today's budget it still adds 4.3 points. The change comes from reallocation
  alone (fewer spare jet engines, more cheap long-turnaround rotables such as rotor blades, gearboxes
  and actuators), and it beats policy P1 (+6.3 points with more stock and 59 % more removals).
* **Demand forecast.** Prediction-driven forecasts (HUMS risk, Weibull age, NFF, life limits) beat
  history-only methods modestly: weighted error 39.8 % vs 43.6 % for SBA overall, 70.5 % vs 75.8 % for
  parts above INR 10 lakh. They run 6-14 % low, and 90 % intervals cover 87 %.
* **AOG risk list.** Units likely to need a part within its lead time at bases without one, with
  "transfer" vs "expedite" advice.

**Planner** (CP-SAT, 48 aircraft x 90 days, 144 jobs)

* Solves to proven optimality in about 10 s; the target was under 60 s.
* Against the due-date rule:
  * normal task: 640 vs 695 aircraft-days down, 115 vs 126 inductions, 29 of 33 predicted removals
    bundled into inspections;
  * +40 % surge: flying-task shortfall 2 vs 14 aircraft-days.
* Remaining lateness is set by spares, not scheduling. A tail-rotor blade at its life limit with no
  spare for 81 days grounds H2009 for 60 days under any schedule; the AOG list flags it as
  "expedite".

**Dashboard.** New Planner tab (optimised vs due-date, availability vs requirement, per-aircraft
timeline with reasons) and Spares tab (cost vs availability curve, simulator validation, AOG risks,
buy / reduce recommendations).

**Fixed along the way**
* *Lead times:* the first procurement lead-time estimate missed new units that went straight to a
  backorder, which inflated battery lead time to 87 days against a true 45. It is replaced by
  Little's law on the stores records (mean on order / order rate), which matches the truth.
* *Weibull minimum:* a Weibull fit on 3 failures made an oxygen concentrator look certain to fail; fits
  now need at least 10 failures, else MTBF is used.
* *Life limits without spares:* in the planner these were first hidden by the baseline (pushed past
  the horizon) and forced to a fake date by the optimiser. They are now modelled as groundings in both
  plans, and work that cannot happen within the horizon counts as late to its end.

**Known limitations (open)**
* METRIC here is single-echelon with pooled stock (the simulator's lateral transfers make that
  reasonable); a two-echelon base / depot model (VARI-METRIC) is the production version.
* The planner keeps each aircraft's flying rate fixed: it plans maintenance, not sortie allocation, so
  phase-flow balancing is not yet a decision.
* Higher inspections do not yet absorb lower ones due at the same time.
* The planner's value is measured in its own plan accounting. Executing plans in the simulator is
  Phase 6 (policy P2). *(Done: section 19.)*
* Still open from Phase 3: the fleet engine RUL intervals under-cover. The spares forecast uses them
  (engine removal probabilities) and the planner uses the watch / alert flags; the METRIC result does
  not depend on them (it uses historical rates). *(Improved in Phase 6: section 19.)*

---

## 19. Results of Phase 6 (2026-10-05)

Full numbers: [docs/VALIDATION.md](VALIDATION.md) sections 12-14 (and section 5 for the engine model).
Reproduce: `bash tools/phase6_pipeline.sh` (about 1.5 hours; the study is resumable).

**First, the engine intervals (open since Phase 3).** The engine model now also trains and calibrates
on copies of each C-MAPSS trajectory that start part-way through life, which is how most fleet
engines first appear in the records. Official test sets are unchanged (RMSE 15.4 / 25.5 / 15.6 / 26.3).
On the unseen fleet run, interval coverage in the last 125 cycles rose from 65 % to 80 % (both with
and without the age in the records), RMSE fell from 116 to 108 FH and the bias with the age known from
17 to 8 FH. Inside the simulated fleet, where the records always give the age, the lower bound holds
in about 90 % of predictions.

**P2, the predictive policy** (`airpower/fleetsim/predictive.py`). The platform's own models run
inside the simulation on the HUMS records it would receive: the engine trend model (A1), the battery
trend (A4), the per-serial gearbox detector fitted on a separate training fleet (A3 / A5); plus life
limits and demand rates from the technical records. Never the simulator's truth. P2:
1. draws the spare for a predicted removal ahead of time (a "kit"), from stock a week ahead or as a
   backorder up to a repair pipeline ahead; kits queue behind aircraft already grounded, and a held
   kit not needed soon is handed to a grounded aircraft;
2. times removals: a monitored unit found at an inspection flies on, within its prediction's lower
   bound, until its spare is in hand; then it is replaced at the next inspection or in a short
   planned job. As in practice, a HUMS alert leads to a removal only after an inspection of that
   position confirms the fault (found with probability 0.9 once it is past its P point); an alert the
   check does not confirm costs the diagnosis time on the ground, is not acted on for 14 days, and
   its spare goes back to stock;
3. plans inductions every week with the Phase 5 CP-SAT model built from the fleet's state (28 days);
4. positions spares at the base where the records predict demand;
5. flies aircraft whose removal is pending and whose spare is not in hand last.
False alarms cost what they cost: checks keep aircraft on the ground and use labour, and spares are
reserved until the alert is cleared. Stock is sized as for P0, so P2 is compared on the same inventory.

**Study design.** 30 paired simulated years per arm (seeds 201-230, never used to build a model), each
after a one-year warm-up under the same policy, with two exercises in the measured year. Common
random numbers: one random stream per aircraft, per part number and for the flying task (opt-in in
FleetSim, so Phase 1-5 runs reproduce exactly), which narrowed the paired intervals by about a quarter.

**Result (30 paired years each; the full table, with labour, repair cost and every arm, is VALIDATION.md section 12).**

| Policy | Availability | vs P0 (pts, paired, 95 % CI) | Flying task met | Awaiting spares | In-service failures / yr | Removals / yr | Stock (INR cr) |
|---|---|---|---|---|---|---|---|
| P0 reactive (today) | 75.2 % | - | 98.1 % | 12.4 % | 306 | 826 | 86 |
| P1 preventive, fixed intervals | 81.3 % | +6.2 +- 0.8 | 99.1 % | 5.8 % | 141 | 1343 | 151 |
| P2 predictive, same stock | 76.5 % | +1.4 +- 0.8 | 98.4 % | 11.3 % | 276 | 834 | 86 |
| P0 with the METRIC allocation | 81.7 % | +6.5 +- 0.8 | 99.1 % | 5.9 % | 309 | 831 | 77 |
| P2 with the METRIC allocation | 84.6 % | +9.5 +- 0.8 | 99.3 % | 2.9 % | 281 | 836 | 77 |

* **Stock reallocation is the big lever.** The METRIC allocation of Phase 5 alone gives +6.5 +- 0.8 points
  with 11 % less stock (the Phase 5 check on other seeds: +7.1 +- 1.0). P1 gets +6.2 but needs 74 % more stock
  and 63 % more removals.
* **Prediction adds a smaller gain on top.** P2 at the same stock: **+1.4 +- 0.8 points**, in-service failures
  306 -> 276 a year. On the METRIC stock P2 adds **+2.9 +- 0.6 points** over P0 on that same stock, and the
  two together reach 84.6 % against 75.2 %, **+9.5 +- 0.8 points**, with 11 % less inventory.
* **Even perfect prediction is worth only +1.7 +- 0.9 points at the same stock**, because the models act on
  engines, gearboxes, batteries, rotor blades and cartridges; most of the time lost waiting for parts is
  random failure of avionics and hydraulics, which only stock (and time) addresses.

**Value of prediction** (VALIDATION.md section 13): each model's own error multiplied by k, availability
gain over P0 at the same stock.

| Prediction quality | Engine RMSE, last 125 cycles | Gearbox AUC | Gain vs P0 (pts) |
|---|---|---|---|
| Perfect (k = 0) | 0 | - | +1.7 +- 0.9 |
| As built | 77 FH | 0.82 | +1.4 +- 0.6 |
| Errors x1.5 | 122 FH | 0.76 | +0.3 +- 0.7 |
| Errors x2 | 167 FH | 0.69 | **-1.1 +- 0.9** |
| Errors x3 | 250 FH | 0.53 | **-5.6 +- 0.6** |
| No HUMS (life limits and records only) | - | - | +0.5 +- 0.7 |

The gain is gone at 1.5 times the error and negative beyond: a poor predictor costs more than it saves,
because every false alarm costs an inspection and aircraft time even though a removal needs a confirming
inspection first. **The models must stay within about 1.5 times their measured error (engine RMSE about
120 FH, gearbox AUC about 0.75) for prediction to pay.** That is the requirement to put to a buyer.

**What each part of P2 adds** (paired differences; each is small beside the noise): spares positioning
+1.3 +- 0.9, the HUMS predictions +1.0 +- 0.9 over records alone, the weekly CP-SAT plan nothing measurable (-0.04 +- 0.86). Only P2
as a whole (+1.4 +- 0.8) and P2 on the METRIC stock (+2.9 +- 0.6) are distinguished from zero. The CP-SAT plan is
kept for the planner view and for surge planning (Phase 5), where its value is in plan accounting; in the
simulated fleet crews and bays rarely bind, so planning inductions earlier changes nothing measurable.

**Fleet twin and what-if.** `airpower/twin/whatif.py` starts FleetSim from the store's state at the
end of the records: airframe hours, every inspection's last done, every fitted component's age,
stock per location, units in repair and on order, and aircraft already down. Remaining lives are drawn
conditional on the recorded age and on not having been found at the inspections since. Levers:
flying rate, an exercise, repair and procurement times, capacity per maintenance level, stock (held or
METRIC) and policy; every scenario is paired with the baseline on the same seeds. The API runs it as
a background job (`POST /api/whatif`).

**Checked against what happened next** (VALIDATION.md section 14): from each of three stores the twin
forecast 30 days and the same simulated run was continued past the end of its records.

| Records | Forecast availability (sd of one future) | Actual | Failures forecast / actual |
|---|---|---|---|
| 1 year | 73.7 % (2.9) | 76.7 % | 24 / 22 |
| 3 years, seed 2 | 72.1 % (2.3) | 70.5 % | 26 / 25 |
| 3 years, seed 3 | 78.0 % (1.9) | 81.4 % | 25 / 16 |

All three actual values fall inside the 95 % band; the forecast misses by 3.0, 1.6 and 3.4 points (mean absolute
error 2.7; the mean signed error is -1.6, a slight pessimism). It over-forecasts backorders. This is a check on a
simulated fleet, not on real data.

**Fixed along the way.**
* *Value-of-prediction cliff:* in the first study, doubling the models' errors cut availability to
  57 % and tripling them to 29 %. The degraded intervals reached their lower bound on healthy engines
  and gearboxes, P2 removed them, and the shop could not return them fast enough; spares drawn for
  alerts that never materialised were also never given back. That measured P2's rules, not
  prediction. Removals now need a confirming inspection, and spares go back when an alert clears;
  the study was rerun from scratch.
* *Twin start-up bias:* the first twin forecast 64.5 % availability against 76.7 % actual, with failures
  at 160 % of actual. Two causes, both in drawing lives from recorded ages: rogue serials were drawn at
  random, so old units came out rogue and failed at once (now the posterior probability of being rogue
  given the age), and units already inside their P-F window were accepted too readily (now screened
  against the inspections the aircraft's records say they survived).
* *Planner in the loop:* the first version pulled 331 inspections early in two years and lost 1.4
  points: an over-capacity day cost as much as a flying-task shortfall, and inspections planned on
  their estimated due day were started before they were really due. Over-capacity now costs about a
  day of downtime, and only deliberate early inductions are executed.
* *Engine edge case:* a unit already found at an inspection could fly on past its life limit when the
  finding was deferred; life limits are now checked after the policy's decision.
* *Memory:* sixteen workers holding full HUMS logs exhausted the 15.6 GB machine and killed the
  process pool; studies now run without logging HUMS records (the policy still receives every one).

**Known limitations (open).**
* P2 acts only where there is a sensor or a life limit (engines, gearboxes, batteries, rotor blades,
  cartridges). Random failures of unmonitored avionics and hydraulics are untouched; the METRIC stock is what
  moves those.
* The value-of-prediction curve degrades the models in a stated way (error multiplied by k; noise on the
  gearbox health index). The curve reports the quality it actually produced (engine RMSE, gearbox AUC).
* Individual pieces of P2 are not separately significant at 30 years per arm; more replications would narrow them.
* The twin over-forecasts backorders and runs about 1.6 points low on average.
* P2's confirmation step (a check finds a fault past its P point with probability 0.9, and an unconfirmed alert
  is left for 14 days) is a modelling choice about maintenance practice, not a measured quantity.

---

## 20. Results of Phase 7 (2026-10-05)

All section 8 views run on the live API. Demo walkthrough for a first-time presenter:
[docs/DEMO.md](DEMO.md).

| Section 8 view | Where | What is on it |
|---|---|---|
| Readiness | *Readiness* | Serviceable now per unit and why the others are down; 12-month history; **30-day fleet-twin forecast with its 10-90 % band**; parts awaited; HUMS components at risk; every aircraft (click for its page); fleet report |
| Fleet health map | *Health map* | Tails x systems (engines, gearboxes, rotor blades, battery, escape system, open defects, next inspection, spares): worst of HUMS risk and life consumed, reason on hover, click for the aircraft |
| Tail page | `#aircraft/<tail>` | The aircraft twin: recommended actions with their reasons and source, predicted remaining life with 90 % intervals, life-limited parts (consumed, due date), inspections due, HUMS trends, 90-day status timeline, defect history filterable by ATA chapter and text, configuration; printable report |
| Planner | *Planner* | Optimised vs due-date plan, availability vs requirement, per-aircraft timeline; **accept or move** the next 14 days' jobs, recorded with user and time; the next plan run keeps them on that date |
| Spares | *Spares* | Cost vs availability curve, simulator check, AOG risks, buy / reduce list (Phase 5) |
| Records | *Reliability* | Per-part reliability, Weibull pattern, repeat defects, rogue serials, defect search (Phase 4) |
| What-if | *What if* | Presets and levers (flying rate, exercise, repair and procurement times, crews / bays per level, stock, policy) -> forecast vs today's settings on the same simulated futures, difference with 95 % CI |
| Evidence | *Evidence* | The Phase 6 policy table, value-of-prediction curve, what each part of P2 adds, the twin's forecast check |
| Data integration | *Data integration* | Source freshness, entity resolution, every data-quality issue and its resolution (Phase 2) |
| Reports | `/report/fleet`, `/report/aircraft/<tail>` | A4 HTML reports from the same data (AeroTwin's renderer adapted); the browser's print saves them as PDF |

**Usable without a briefing.** Every view opens with a three-line "how to read this" note on first
visit (reopen it from the view); every recommendation says why and which model or record raised it;
numbers with uncertainty show it (remaining-life intervals, forecast bands, confidence intervals on
differences); aircraft pages have stable links.

**API added.** `/api/healthmap`, `/api/aircraft/{tail}/twin`, `/api/forecast`, `POST /api/whatif` +
`/api/whatif/{id}` (background job, results cached by store, levers, horizon and replications),
`/api/study`, `POST /api/plan/decision` + `/api/plan/decisions`, `/report/...`.

**Known limitations (open).**
* What-if jobs run in the API process's own worker pool: fine for a demo, a job queue and limits
  per user belong to Phase 8 with RBAC and the audit log.
* Reports are HTML printed to PDF by the browser, not a server-side PDF.
* The health map takes about 4 s to build the first time for a date (then cached).

---

## 21. Results of Phase 8 (2026-10-05)

Hardening and the submission material. The security model, with its gaps, is [SECURITY.md](SECURITY.md);
install, run, back up and upgrade are [OPERATIONS.md](OPERATIONS.md).

| Area | What exists | Evidence |
|---|---|---|
| Roles | Five roles (commander, engineering officer, logistics officer, analyst, administrator) with least-privilege permissions; the administrator manages users and reads the audit log and cannot decide plans, run what-ifs or export reports | `tests/test_security.py` signs in as each role against every restricted route |
| Access control | Deny by default: one table of (method, path, permission) rules for all 37 routes; a test fails if a route has none; role and disabled state re-read on every request | `test_every_route_has_a_rule`, `test_roles`, `test_disabled_user_and_role_change_apply_at_once` |
| Sign-in | scrypt, 12-character minimum, temporary passwords must be changed, five failures lock an account for five minutes, ten per address are refused, the same message and the same work for unknown users, signed HttpOnly SameSite=Strict session cookie, CSRF header on every state-changing call | tests as above |
| Audit log | Append-only (the database refuses UPDATE and DELETE) and hash-chained; sign-ins and failures, decisions, what-if runs, report exports, user changes, data loads, refused requests; *Verify the chain* in the dashboard and on backups | `test_audit_chain_and_tamper_evidence`; edited row found in the chain |
| Request hardening | Content security policy without inline script, `nosniff`, frame denial, no-store caching, body size limit, validated what-if levers, one forecast per user and two per server, no API documentation when sign-in is on, generic errors with a request id, no path traversal out of the static folder | tests; checked in the running dashboard |
| Signed HUMS uplink | Field validation added to AeroTwin's HMAC envelope (timestamp window, replay cache); refusals audited | `tests/test_integration.py` |
| Model files | SHA-256 manifest of every trained model, optional HMAC signature; loading refuses a changed or unlisted file | `test_model_files_are_verified_before_loading`, `test_signed_manifest` |
| TLS | Proxy profile (nginx) and direct serving; HSTS; Secure cookie flag; address behind a proxy believed only from a trusted network | Served over HTTPS with a generated certificate; Secure cookie and forced password change checked with curl |
| Containers | `deploy/Dockerfile` (multi-stage, unprivileged user), compose file with app, TLS proxy and PostgreSQL / TimescaleDB profiles, secrets as files, read-only file system, capabilities dropped | Compose file passes `docker compose config`. **The image, proxy and PostgreSQL were not run: no container engine was running.** CI builds the image and starts it |
| Data load | Users, plan decisions and the audit log survive every load; the integration report is written last and `/api/ready` is false until then; each load is audited; migrations of the operational tables | `test_data_load_keeps_users_decisions_and_audit`, `test_migration_of_an_older_store` |
| Operations | Backup and restore with integrity and audit-chain checks, secrets and demo-certificate setup, structured request log, readiness and liveness endpoints, background warm-up of the first health map | `test_backup_restore_roundtrip`, `test_demo_cert_and_secrets` |
| Dependencies | `pip-audit` and `npm audit` run; starlette 1.0.0 -> 1.3.1, cryptography 46.0.7 -> 50.0.2 and echarts 5.6 -> 6.1.0 upgraded for advisories; licence register generated from package metadata and the data manifest | SECURITY.md section 5; THIRD_PARTY.md; CI `audit` job |
| Clean install | `tools/clean_install_check.sh` copies the repository to a scratch directory, installs into a new environment from `requirements.txt`, runs the tests that need no datasets, simulates and integrates 90 days, starts the server with a bootstrap administrator, signs in, changes the temporary password and reads data | **Passed** (2026-10-05, about 6 minutes, with the upgraded pins): the 63 tests that need no datasets, 90 simulated days integrated with all 11 injected fault types handled, server started, bootstrap administrator signed in, temporary password changed, data read, audit chain intact |
| Documents | SECURITY (STRIDE), ARCHITECTURE, OPERATIONS, DATA_PLAN, FMEA, ROADMAP, SIH_TRACEABILITY, THIRD_PARTY (generated), DEMO (updated for roles), README | `docs/` |
| Dashboard | Sign-in, role-aware tabs, password change, an Administration view (users, audit log, chain check), classification banner on every screen and report, compact header on small screens | Checked in the browser as engineer, commander and administrator |

**Fixed along the way.**
* *Decisions would have been erased by every data load.* The loader's full refresh dropped every table in the schema,
  including `plan_decision`; it would have destroyed the audit log too. Operational tables are now separate, kept
  across loads and versioned (`airpower/model/migrate.py`).
* *Dependency advisories.* The audit found known vulnerabilities in the pinned starlette, cryptography and echarts (section 5
  of SECURITY.md); all three were upgraded, the tests re-run and the dashboard checked.
* *Pickled models.* Loading a model file runs code, so model files are now checked against a manifest before loading.
* *Sign-in throttling behind a proxy.* With every user arriving from the proxy's address, ten failed sign-ins by anyone would
  have locked everyone out for ten minutes; the client address is now taken from the proxy, and only from a trusted one.
* *Smaller items:* the unauthenticated health endpoint disclosed the loaded run (now `{"status": "ok"}`); the single-page
  fallback would have answered unknown API paths with the dashboard page (now 404); uploads with no declared length were not size-checked.

**Deviations from the plan.**
* *Alembic* is replaced by a small migration runner for the three operational tables: the derived schema is rebuilt on each
  load and carries no history, so versioning applies only to the operational tables.
* *Data classification* is a label per store (shown on every screen, report and API response), not on every record: all records in
  a store share a source. Per-record labels would be inherited from the source systems.
* *Incremental loads:* a load rebuilds the integrated tables from the cumulative exports of the four systems and keeps the operational
  tables; live HUMS arrives through the signed uplink. A load into a new database with switch-over is on the roadmap.
* *Reports* are HTML printed to PDF by the browser (reportlab is not used).
* The submission deck is planned separately (`docs/PPT_PLAN.md`) and is not part of this phase's code.

**Known limitations (open).** Everything in SECURITY.md section 6: no multi-factor or smart-card sign-in, demo keys, SQLite not encrypted,
no penetration test, audit log not yet forwarded to a separate service. The container image, TLS proxy and PostgreSQL profile have not been run.
Tests: 74, all pass (63 need no datasets; 11 use the datasets or a loaded store).
