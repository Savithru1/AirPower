# AirPower: data plan

What data each part of the system uses, where it comes from, what is real and what is assumed, and what
an operator must supply to run on a real fleet. Dataset terms are generated into
[THIRD_PARTY.md](THIRD_PARTY.md) from `data/manifest.json`.

## 1. What is real and what is synthetic

| Layer | Source | Real? |
|---|---|---|
| Component health models (A1-A4) | NASA C-MAPSS, CWRU and SEU vibration, NASA Ames batteries, NGAFID flights | Real or NASA-simulated public datasets, scored on units and conditions held out from training |
| Defect text model (A7) | FAA Service Difficulty Reports 2020-2025, 376,311 reports | Real, civil aviation, temporal train / test split |
| Fleet (aircraft, components, defects, stores, agencies) | FleetSim | **Synthetic.** Every parameter is an assumption in the YAML configuration |
| HUMS signals inside the fleet | Replayed C-MAPSS fleet units, CWRU / SEU windows, NASA battery cells | Real signals, assigned to simulated components |
| Source-system exports | FleetSim, deliberately fragmented | Synthetic; each injected fault is logged so the integration is scored against truth |

No military data was used and none is in the repository. The two aircraft types do not represent any
in-service aircraft.

## 2. Datasets

| Dataset | Used for | Held out | Terms |
|---|---|---|---|
| NASA C-MAPSS FD001-FD004 | A1 engine RUL; FD001 / FD003 trajectories replayed as engine HUMS | 40 % of FD001 / FD003 units (seed 26249) are reserved for the simulator and never seen by a model; official test sets score A1 | US Government work, public |
| NGAFID 2-day subset (11,446 flights) | A2 pre-maintenance risk | The dataset's five folds | CC-BY 4.0 |
| CWRU bearing, SEU gearbox | A3 fault detection and type; healthy-to-fault trajectories for gearboxes | Leave one operating condition out | Public research data, cite; SEU not redistributed |
| NASA Ames batteries | A4 battery RUL; capacity-fade curves | Leave one cell out | US Government work, public |
| FAA SDRs 2020-2025 | A7 text to ATA chapter, A8 repeat and rogue analysis on real reports | Train on earlier years, test on later; manual references masked | US Government work, not redistributed |

Large sets stay with AeroTwin and are read in place (`AEROTWIN_DATA`); `tools/fetch_real_data.py` fetches
the rest. `python -m data_prep.sources` reports what is present. Without the optional sets FleetSim uses
parametric signals (CI does) and the run metadata records which was used.

## 3. FleetSim's assumptions

* 48 aircraft: two squadrons of 18 single-engine jets, one of 12 twin-engine helicopters; two bases and a depot.
* Flying five days a week with two exercises a year (task x1.4 for 14 days and x1.5 for 10 days).
* About 25 component positions per type with Weibull lives, P-F intervals (how long before failure a defect can be found), life limits, no-fault-found shares, turnaround and lead times, costs: all in `config/type_J.yaml` and `type_H.yaml`.
* 4 % of rotable serials are "rogue" (life x 0.3). Inspections find a developing fault with a stated probability.
* Stock is sized by a Poisson service level per part (rotables 0.85, consumables 0.97), the usual single-item rule.
* Source exports carry injected faults: duplicate and late defects, missing ATA, serial typos, part-number formats and supersessions, missing links and tails, duplicate and missing HUMS uploads.

Changing a number changes the results; they are meant to be swapped for the unit's own.

## 4. What an operator supplies

| System | What the platform needs | Interface to confirm with the user agency |
|---|---|---|
| Technical records | Aircraft, defects (date, text, ATA if recorded), work done, component life cards (hours, cycles, calendar), inspection history, part lists with serials | Export format of the records system (CSV in the prototype) |
| HUMS | Post-flight downloads with channel values per monitored position, serial, flight start / end, unit and airframe id | Ground-station file format; live feed through the signed HTTP uplink |
| Stores | Stock by location, issues, receipts, returns, repair returns, dues-in, part-number supersessions | Stores system export |
| Maintenance agencies | Work orders, agency, level, open and close times, capacity (crews / bays) | Work-order export; capacity from the unit's rosters |

Adapters for each real format are the first piece of work in the pilot ([ROADMAP.md](ROADMAP.md)). The entity-resolution rules
(tail formats, part-number aliases and supersessions, serial normalisation and typo matching, work-order
links) are configured in `airpower/ingest/registry.yaml`.

## 5. Data protection in the prototype

Data classification is a label per store (`AIRPOWER_CLASSIFICATION`) shown on every screen, report and API
response; per-record labels would come from the source systems. Reports are the only way data leaves,
and each is audited. See [SECURITY.md](SECURITY.md).
