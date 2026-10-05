# AirPower: roadmap

From a prototype on a simulated fleet to a system a squadron can rely on. The first three steps match the
deck plan (Phase A, B, C); the technical backlog is what the build left open. Dates depend on access to
real data and are not given.

## Where it stands

Phases 0-8 of the plan are built: a synthetic 48-aircraft fleet with real NASA and university sensor
signals, four fragmented source systems, an integration layer scored against truth, health models scored on
public data, records analytics, spares and maintenance planning, a predictive policy tested in the simulated
fleet, a fleet twin for what-if, and a dashboard with roles, audit and printable reports. Results:
[VALIDATION.md](VALIDATION.md). Nothing has touched real fleet data.

## Phase A: real adapters

* Agree with the user agency the export of each of the four systems (technical records, HUMS ground station, stores, work orders / agency capacity) and the legal basis for a copy of historical data.
* One adapter per real format into the existing integration layer; extend `registry.yaml` conventions (tail, part-number, serial, time-zone, date formats); keep the data-quality report as the acceptance test: **every inconsistency resolved or reported**.
* Replace FleetSim's assumptions with the unit's: fleet configuration, flying programme, inspection programme, life limits, agency capacity and repair turnaround (all YAML).
* Done when the integration of the unit's historical data reconciles (counts, serviceability derived from records against the unit's own availability reports).

## Phase B: pilot on one squadron's history

* Run in **shadow**: replay the squadron's history, make predictions and plans as if live, and score them against what happened (the same harness as the policy study, with the real record as truth).
* Calibrate and re-validate each model on the unit's data: engine and gearbox models against real removals, the defect-text model fine-tuned on the unit's labelled defects, Weibull and rogue analysis on its removals.
* Measure: warning lead time, false-alarm cost, spares forecast error, planner lateness against what was done.
* Done when the pilot report states, with intervals, what each model would have caught and cost, and the squadron's maintenance staff agree the recommendations are usable.

## Phase C: live

* Advisory first (recommendations beside the existing process), then plan acceptance inside the workflow.
* Live HUMS through the signed uplink; scheduled integration of the other three systems; model monitoring and quarterly re-scoring.
* The production items in [SECURITY.md](SECURITY.md) section 6 are conditions for this phase: unit identity provider and multi-factor, key service, encrypted PostgreSQL, forwarded audit log, independent penetration test.

## Technical backlog (from the build)

| Item | Why |
|---|---|
| Two-echelon spares model (VARI-METRIC) | The allocation today is single-echelon with pooled stock |
| Sortie allocation in the planner (phase flow, hours balancing across aircraft) | The planner keeps each aircraft's flying rate fixed |
| Higher inspections absorbing lower ones due at the same time | Open in the planner |
| Deep sequence model for engine RUL (needs PyTorch, deferred) | A1 reaches about 15 cycles RMSE on FD001 (15.4) against about 13 for the best published sequence models |
| Model monitoring: drift and calibration alerts | Today re-scoring is a manual run of the study harness |
| Load into a new database and switch over; incremental loads | Today a load rebuilds the integrated tables |
| Run and fix the container image, TLS proxy and PostgreSQL profile | Written and configuration-checked, not run in the build environment |
| Server-side PDF reports | Reports are HTML printed to PDF by the browser |
| Per-record data classification inherited from sources; row-level access by unit | One label per store today |
| A MALE UAV type whose engine health comes from AeroTwin | Planned as an optional plug-in |
| N-CMAPSS (real flight conditions) as a harder RUL benchmark | Stretch |
| Job queue for what-if (database-backed, per-user limits) | Needed beyond one server process |

## What would change the plan

Real data that looks unlike the simulated fleet (the point of Phase B is to find out); an agency that cannot
export a system in a usable form; a requirement for a different identity or key infrastructure than assumed.
