# AirPower: failure modes of the platform

What can go wrong in the platform itself (data, models, decisions, system), what it would cause, how it
is noticed, and what limits the damage. This is not a safety analysis of any aircraft: the platform gives
advice and a human decides. Severity is the effect on a maintenance or tasking decision: **H** a wrong
decision that costs aircraft availability or a part, **M** a delayed or degraded decision, **L** an
inconvenience. "Seen" means it happens in the simulated fleet and the handling is tested.

## 1. Data

| Failure | Effect | How it is noticed | Handling | Sev |
|---|---|---|---|---|
| HUMS download not made after a sortie (3 % in the simulated fleet) | Gap in a trend; a slower alert | Missing downloads are listed as data-quality issues, counted per aircraft | Window features tolerate gaps; the engine model has trend snapshots per cycle; flagged, not hidden | L |
| Duplicate HUMS upload | Double counted indicators | Duplicate key (unit, flight start) | Detected and ignored by the signed uplink and the loader (seen) | L |
| Serial number mistyped in a record (4 %) | Part history attached to the wrong unit, or none | A serial not in the life cards, or valid but on another aircraft | Normalised, matched by edit distance and checked against stores issues on the same work order; 72 of 74 fixed in the simulated run, the rest flagged (seen) | H |
| Part number in another format, or superseded | Stock and demand split across two numbers | Alias table misses | Aliases and supersessions resolved; the stock number is inferred from serial votes when a cross-reference is missing (seen) | H |
| ATA chapter missing on a defect (10 %) | Defect not counted under its system | Missing field | Inferred by a classifier trained on the operator's own defects; marked "inferred" everywhere (seen) | M |
| Defect entered late or twice | Wrong order or double counting in repeat-defect analysis | Entry date vs event date; same aircraft, text and work order | Late entries flagged; duplicates merged only when the work order matches (seen) | M |
| Work order not linked to its defect | Maintenance time not attributed | Work order without a defect link on an unscheduled job | Link recovered from component changes and times; the rest listed (seen) | M |
| Different time zones or date formats between systems | Events out of order | Impossible sequences (a part issued before it was demanded) | Conventions per source in `registry.yaml`, converted to UTC (seen) | H |
| A new export format the adapters do not know | Load fails or loads nothing | The integration report's per-source counts | The loader reports counts per source and stops on a missing required column; adapters are the first pilot task | H |
| The store is stale | Decisions on old data | "As of" date on every screen; the Data integration view shows when it was loaded | Nothing forces a refresh; set a schedule (OPERATIONS.md) | M |

## 2. Models

| Failure | Effect | How it is noticed | Handling | Sev |
|---|---|---|---|---|
| Engine remaining life too optimistic (age unknown, short history) | A late removal | Coverage of the 90 % interval measured on an unseen run: 80 % (it was 65 % before mid-life training) | The interval is shown and plans use its lower end; the age comes from the life card when present | H |
| Gearbox alarm on a healthy unit | An unnecessary check or removal | False flags are measured: 0.2 % of healthy records in the unseen run | A removal needs a confirming inspection first; an alert that is not confirmed is not acted on again for 14 days and its spare is released (policy P2) | M |
| Gearbox fault missed | The unit flies on until the next inspection or failure | Detection of 53 % of degrading lives in the unseen run | Inspections still find faults inside the P-F window; this is why the gain from prediction is reported as measured, not assumed | H |
| Battery trend wrong | Early or late change; a battery costs little | Interval x 3.9 either side, shown | Low consequence; wide interval displayed | L |
| Defect-text model meets a logbook style it was not trained on | Wrong ATA chapter | Top-1 90.3 % on civil reports, 66 % on the simulator's template text | Fine-tune on the operator's labelled records (the integration already trains on the operator's own defects) | M |
| Model drift over years | Quietly worse predictions | Not detected automatically | Re-score on new failures every quarter; the study harness (`airpower.twin.study`) runs the same check; monitoring is a roadmap item | H |
| A model file replaced or corrupted | Code runs, or wrong predictions | Hash does not match the manifest at load | Loading is refused (SECURITY.md 3.4) | H |

## 3. Decisions

| Failure | Effect | How it is noticed | Handling | Sev |
|---|---|---|---|---|
| Planner has no feasible plan | No recommended schedule | Solver status on the Planner view | Falls back to showing the due-date plan; the time limit returns the best plan found | M |
| A spare is not there when the plan needs it | The aircraft waits (NMCS) | The grounded list and the AOG list say so in advance | Plans model aircraft as grounded until the spare arrives; the remedy (transfer or expedite) is shown | M |
| Forecast too narrow or biased | Overconfidence in the next weeks | Checked against what the simulated fleet did next: three of three inside the 95 % band, about 1.6 points low on average | The band, not a single number, is shown; the check is on the Evidence view | M |
| An engineering officer moves a job and the plan then violates a limit | A late inspection | Decisions are logged; the next plan keeps the date and reports lateness it causes | Advice only: a person decides and is recorded | M |
| Someone reads "predicted" as "certain" | A part removed too early or kept too long | Every prediction carries an interval and the reasons | Guides on each view; reports state that outputs are decision support | M |

## 4. System

| Failure | Effect | How it is noticed | Handling | Sev |
|---|---|---|---|---|
| API process stops | Dashboard unavailable | `/api/health`; the container health check | Restart policy; nothing is lost (the store is on disk) | M |
| A data load fails part-way | An incomplete store | `/api/ready` stays false until the load finishes | Users and the audit log are kept; load again. A switch-over load into a new database is a roadmap item | M |
| SQLite file corrupted | Store lost | Integrity check on backup | Daily online backup with an integrity and audit-chain check; restore drill in OPERATIONS.md | H |
| Disk full | Writes fail, audit entries refused, plan decisions refused | Errors on writes | A decision that cannot be audited is refused rather than taken unrecorded | M |
| Worker pool dies under memory pressure (seen during development) | A what-if or study fails | The job reports failure | A fresh pool is started once; studies resume from the last finished run | L |
| Server restarted without a session key | All users signed out | 401 | Set `AIRPOWER_SECRET` | L |
| HUMS signing key leaked | Forged downloads accepted | Signed uploads from an unexpected address; audit | Rotate the key; per-station keys are a security roadmap item | H |
| Clock wrong on the server | Replay window and audit times off | Uploads refused as stale; times out of order | Time synchronisation (SECURITY.md section 6) | M |
