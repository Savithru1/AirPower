# AirPower: SIH 2026 idea-submission deck, plan

Problem statement **26249**, *Air Power: Predictive Maintenance & Fleet Availability* (Ministry of
Defence, Defence Services Staff College). Team **Gandhiva**. Plan v1, 2026-10-05.

This is the AirPower counterpart of the AERO-TWIN deck (`F:\AeroTwin\AERO-TWIN_SIH2026_Gandhiva_v3.pptx`).
It fixes the slide order, the picture on every slide, and the exact numbers, with the file each number
comes from. **Six slides only (no system-flowchart slide).** Nothing is built until the team supplies the Vercel link.

---

## 0. What carries over from AeroTwin

| Rule | Why |
|---|---|
| **6 slides in the SIH template order**: title, Proposed Solution, Technical Approach, Feasibility and Viability, Impact and Benefits, Research and References | The AeroTwin deck followed the previous winner's flow and the template had to be "strictly followed" |
| **Same look**: Gandhiva oval top-left, SIH 2026 logo top-right, serif title in the middle, blue footer bar with the page number, white cards with a thin coloured border, soft-tinted icon circles, native shapes only (editable) | The two decks read as one team's work |
| **Pictures, not paragraphs**: every line is a fragment of at most 6 words, or a big number with a 3-word label | The brief was "not much words, pictographically explained" |
| **Original wording**: no sentence copied from the problem statement, the template, a paper title or the AeroTwin deck. Papers go in as a short description + journal + year + link | Two plagiarism rounds (27 % and 25-30 %) came from tech names, template phrases and paper titles |
| **Footer boilerplate removed** (the "Idea submission - Template" line) | It appears in thousands of SIH decks |
| **Every number has a source and a label: real data or simulated fleet** | AeroTwin's draft shipped invented figures (a "70 % built" bar, a budget split) and unchecked market statistics; this deck does not |

What changes: AeroTwin is one engine on one UAV (physics, CAN bus, radio link). AirPower is the **fleet
layer**: four unconnected record systems joined into one model, predictions turned into spares and
maintenance decisions, tested in a fleet digital twin. So the "UAV / link / ground" lanes become
"sources / integration / decisions", and the hero picture is a dashboard, not a 3D aircraft.

**Design element (the one idea to remember):** *four silos become one fleet picture*. A single motif,
four small coloured boxes (HUMS, Technical records, Spares, Agencies) flowing into one big box, repeats
on slides 2 and 3, the way the "Model, Shadow, AERO-TWIN" staircase did in AeroTwin.

**Colour code (reuse everywhere):** blue = records / integration, orange = HUMS / health, green = spares,
purple = planner / agencies, red = risk / impact.

---

## 1. Slide 1: title

Same layout as AeroTwin slide 1 (SIH logo and brain image from the template).

| Field | Value |
|---|---|
| Problem Statement ID | SIH26249 (AeroTwin used the "SIH" prefix; confirm the portal's form) |
| Title | Air Power: Predictive Maintenance & Fleet Availability |
| Theme | Transportation & Logistics (from `IMPLEMENTATION_PLAN.md`; confirm on the portal) |
| PS Category | Software |
| Team ID | **[needed from the team]** |
| Team Name | Gandhiva |

Template fields must match the portal exactly; do not reword them.

---

## 2. Slide 2: Proposed Solution

**Title in the middle:** AIRPOWER (the "AIR" dark, the "POWER" orange, like AERO-TWIN).

**Left card, "Proposed Solution" (6 icon rows):**

| Icon | Text (fragment) |
|---|---|
| link / merge | **4 systems, 1 fleet model** |
| hourglass | **Remaining life** per part, with a 90 % band |
| search | **Defects read** and filed by ATA chapter |
| box | **Spares** placed by METRIC, not by habit |
| calendar | **90-day plan** in 10 s, bundled jobs |
| monitor | **Fleet twin**: forecast and what-if |

**Left-bottom:** two dashboard screenshots with captions, *Readiness* ("30-day forecast") and *Health map*
("red = act now"). Captured from the running dashboard (`python -m server.app`).

**Centre (hub diagram, like AeroTwin's 8 cards around the aircraft):** a round picture of the Readiness view
in the middle, 8 small cards around it:

1. 48 aircraft, jets + helicopters
2. HUMS · tech records · stores · agencies
3. Entity resolution (tails, part numbers, serials)
4. Engine RUL, 90 % interval
5. Pre-maintenance risk per flight
6. Gearbox fault vs own baseline
7. Rogue serials + repeat defects
8. What-if before you decide

Quote under it: *"Fix the plan before the part fails, not after the aircraft waits."* (own wording)

**Right card, "Why It Works" (4 big numbers, coloured):**

| Number | Label | Source |
|---|---|---|
| **+7.1 pts** | availability, same fleet | VALIDATION §8 |
| **-11 %** | spares stock value | VALIDATION §8 |
| **97.8 %** | aircraft-days read right from messy records | VALIDATION §10 |
| **90.3 %** | defect text filed to the right ATA chapter, 134k real reports | VALIDATION §6 |

**Right-middle strip (4 icon tiles):** Jet + helicopter · On-prem · Air-gapped · Role-based screens.

**Right-bottom staircase "From silos to decisions":** *Silos* (grey, today) → *Joined* (blue-grey) →
*AirPower: predicts · plans* (orange).

Honesty line in small print at the bottom: "fleet is simulated; its sensor signals are real NASA and university data".

---

## 3. Slide 3: Technical Approach

**Left card, "Built With" (logo chips, 5 per row, same style as AeroTwin):**

| Group | Chips |
|---|---|
| Simulate & learn | Python · SimPy · NumPy · pandas · PyArrow |
| Predict & explain | scikit-learn · ProgPy · SHAP · SciPy · RapidFuzz |
| Plan & optimise | OR-Tools CP-SAT · METRIC (Sherbrooke) · Croston / SBA · Monte Carlo |
| Server & UI | FastAPI · SQLAlchemy · SQLite (PostgreSQL / TimescaleDB supported) · React · ECharts |
| Ship & test | Docker · GitHub Actions · pytest · Git · TypeScript |

Footer line: **all open source → ₹0 in licences** (checked against `THIRD_PARTY.md`: MIT / BSD / Apache; ProgPy is NASA-1.3; psycopg is LGPL).

**Top-middle box, "Prototype running live: open demo":** link, plus a *progress strip*. Do **not** use a made-up
percentage. Use the real one: **All 9 phases done** (Phases 0-7: setup · fleet simulator · integration · health
models · records & reliability · spares & planner · policy study & twin · dashboard; Phase 8: hardening and security), bar full.

**Top-right strip (replaces AeroTwin's "78 kbps → 3.3 kbps" link strip), "Messy in, clean out":**
`4 systems` → `11 fault types injected, 2,061 faults` → `integration layer` → `2,058 fixed` (99.9 %) → `10,234 / 10,234 HUMS downloads on the right sortie`.
Source: VALIDATION §10.

**Main flowchart, two lanes (same dashed-lane look as AeroTwin slide 3):**

*Lane 1, "INTEGRATE" (blue):*
`HUMS post-flight files` · `Technical records (text defects)` · `Spares / stores` · `Agency capacity & work orders`
→ `Adapters` (one per source) → `Entity resolution` (tail, part number, serial) → `Unified fleet store` + data-quality report.

*Lane 2, "PREDICT & DECIDE" (green):*
`Risk records` (one per aircraft position: removal-time distribution, confidence, explanation)
→ `Spares` (demand + METRIC allocation + AOG list)
→ `Planner` (CP-SAT, 90 days, accept / override)
→ `Fleet twin` (forecast with band, what-if)
→ `Role dashboards`.

---

## 4. Slide 4: Feasibility and Viability

Three header bars like AeroTwin: *Cost & Revenue* (left), *Feasibility* (middle), *Viability* (right),
with *Risks → How We Handle Them* under the middle and right columns.

**Cost & Revenue (left):**

- 4 revenue tiles (icon + 2 words + a grey subtitle): **Per-fleet licence** (each fleet) · **Depot analytics** (on-prem) · **Records adapters** (real HUMS / stores formats) · **Training & support**. These are the team's business-model choice; confirm before printing.
- In place of AeroTwin's "pilot budget split" doughnut (those percentages were invented), a **real chart**: *same fleet, more availability* (3 bars, from VALIDATION §8):

| Stock | Value | Availability |
|---|---|---|
| Today's rule | ₹86.4 cr | 75.5 % |
| METRIC, same budget | ₹77.2 cr | 82.5 % |
| METRIC, 75 % budget | ₹62.9 cr | 79.8 % |

- Green box: **₹0 on licences, open tools only.**
- Optional (only if the team supplies real figures): a pilot-budget doughnut.

**Feasibility (middle, 4 rows):**
- **Works today**, records to decision
- **Plan solved in 10 s** (target 60 s): 48 aircraft × 90 days
- **Real data in hand**: 376,311 FAA reports, 11,446 flights
- **Open tools only**, no CDN

**Viability (right, 4 rows):**
- **Advice only**: planner recommends, officer accepts or overrides, every decision logged
- **Role-based access** + audit log *(built and tested in Phase 8)*
- **On-prem, air-gapped** Docker deploy
- **Swappable fleet**: generic types today, real formats via adapters

**Risks → How We Handle Them (4 icon pairs):**

| Risk | Handled by |
|---|---|
| No real military data | Each model proven on public real data; fleet is simulated and labelled |
| Simulation proves what it assumes | Models trained on separate seeds; false alarms cost a spare; **value-of-prediction curve** (errors ×1.5, ×2, ×3, no HUMS) |
| Four inconsistent systems | Entity resolution scored against injected truth (2,058 / 2,061) |
| Over-trusting a model | 90 % intervals, drivers shown, human decides |

---

## 5. Slide 5: Impact and Benefits

Same layout as AeroTwin slide 5: round picture in the middle (dashboard Readiness view), **IMPACT** cards on
the left (red borders), **BENEFITS** cards on the right (green borders), two icon rows per card.

**Impact (measured in our simulated fleet):**

| Card | Row 1 | Row 2 | Source |
|---|---|---|---|
| Availability | **+7.1 pts** at the same fleet | **-11 %** spares stock | VALIDATION §8 |
| Waiting for parts | **12.5 % → 5.1 %** aircraft-days | stock moves to rotor blades, gearboxes, actuators | VALIDATION §8 |
| Planning | **640 vs 695** aircraft-days down | **115 vs 126** separate inductions | VALIDATION §9 |
| Surge (+40 % flying) | **2 vs 14** aircraft-days short | 29 of 33 removals bundled | VALIDATION §9 |
| Prediction on top | **+1.4 pts** over reactive at equal stock | **+9.5 pts** with METRIC stock (84.6 % vs 75.2 %) | VALIDATION §12 |

**Benefits (by who uses it):**

| Card | Row 1 | Row 2 |
|---|---|---|
| Command | **Commander:** readiness forecast | **Commander:** what-if before deciding |
| Engineering | **Engineering officer:** health map, ranked actions | **Squadron:** fewer surprise grounded aircraft |
| Logistics | **Logistics officer:** AOG list, buy / reduce | **Depot / BRD:** planned inductions |
| Records | **Analyst:** repeat defects, rogue serials | **Auditors:** every override logged |
| Nation | **MoD:** more aircraft per rupee | **Self-reliant:** home-built, open licences |

**Context cards (top-left, as AeroTwin's "7/10 drone losses"):** only with a figure that has a public source.
Candidates to research and source before use: an Indian fleet serviceability or spares-shortfall figure,
and the cost of aircraft on ground. If none can be sourced, use our own baseline instead:
**24.5 % of aircraft-days not mission capable** in the reactive baseline (12.4 % waiting for spares + 3.6 % maintenance + 8.5 % scheduled; VALIDATION §11).
Do **not** reuse AeroTwin's drone statistics.

---

## 6. Slide 6: Research and References

**Left card, "Literature We Built On" (icon header per group, each paper as description · venue · year · [Link]):**

| Group | Entries |
|---|---|
| Engine life | Turbofan degradation simulation (C-MAPSS), NASA, 2008 · CNN, LSTM and deep-CNN RUL baselines we compare against (Babu 2016, Zheng 2017, Li 2018) |
| Maintenance risk | Aviation maintenance dataset (NGAFID), Yang et al. 2022 [arXiv 2210.07317] · ProgPy, NASA |
| Spares & planning | Multi-echelon recoverable-item model (METRIC), Sherbrooke 1968 · intermittent-demand forecasting (Croston, SBA) · OR-Tools CP-SAT |
| Records | FAA Service Difficulty Reports 2020-2025 · Weibull reliability analysis |
| Standards | ASD S5000F (in-service feedback) · *candidates to verify before adding:* ISO 13374 (condition monitoring data), ISO 13381-1 (prognostics), ASD S3000L (logistics support) |

Write each paper as a description of what it did, never its title, and check the venue and year before printing.

**Centre buttons (like AeroTwin):**
- **Live Demo: click here** → *[Vercel link, pending from the team]*
- **Our Progress: click here** → same Vercel link
- Under them: the 8 dashboard tabs as icons (Readiness · Health map · Plan · Spares · What if · Evidence · Reliability · Data integration)
- Small line: **72 tests · 14.6k lines** (72 collected; recount before printing)

**Prior-art comparison table (right, 7 rows × 5 columns).** Columns: three or four incumbent systems + **Ours**.
Rows: *HUMS + records + spares + agencies in one model* · *Prediction → spares plan* · *Prediction → maintenance
schedule* · *Fleet what-if twin* · *Works air-gapped / on-prem* · *Open licences, Indian IP* · *Evidence on public data*.
Candidate incumbents: Airbus Skywise, Boeing AnalytX, Rolls-Royce / GE engine-health services, and
a generic ERP maintenance module. **Do not tick or cross a competitor cell without a public source**; the
AeroTwin table was ticked by judgement and AirPower's rows are easier to challenge. Source each cell first
(a research pass), or label the table "indicative".

**Bottom-left panel, "Tested on Unseen Data" (big numbers):**

| Number | Label | Source |
|---|---|---|
| **90.3 %** | defect text → ATA chapter (134k later real reports) | VALIDATION §6 |
| **15.4** | engine RUL RMSE, cycles (FD001 official test) | VALIDATION §1 |
| **88-97 %** | of true lives inside the 90 % band | VALIDATION §1 |
| **1.00** | gearbox fault detection AUC vs own baseline (CWRU) | VALIDATION §3 |
| **0.785** | AUC pre-maintenance risk, 11,446 real flights | VALIDATION §2 |

**Bottom-right panel, "Real Data Used":**
**376,311** FAA defect reports · **11,446** real flights (NGAFID) · **4** turbofan run-to-failure sets · **2** vibration sets (bearing, gearbox) · **9** battery cells.

---

## 7. Numbers register

Every number in the plan, where it lives, and whether it can change.

| Number | Where | Status |
|---|---|---|
| +7.1 ± 1.0 pts, 75.5 → 82.5 %, ₹86.4 → ₹77.2 cr, 79.8 % at ₹62.9 cr, 12.5 % → 5.1 % waiting | VALIDATION §8, plan §18 | Final (20 paired years) |
| 640 vs 695 aircraft-days, 115 vs 126, 29 / 33, 2 vs 14 shortfall, 10 s solve | VALIDATION §9 | Final |
| 2,061 injected / 2,058 handled, 97.8 % aircraft-days, 10,234 / 10,234 | VALIDATION §10 | Final (plan §15 says 97.9 % for an earlier run; use 97.8 %) |
| Defect → ATA 90.3 % top-1, 134k test reports | VALIDATION §6 | Final |
| RUL RMSE 15.4 (FD001), 88-97 % coverage; AUC 0.785 / 71.1 %; gearbox AUC 0.99-1.00 | VALIDATION §1-3 | Final |
| Reactive baseline 75.5 % availability, 12.4 / 3.6 / 8.5 % | VALIDATION §11 | Final |
| P0 75.2 %; P1 +6.2 (74 % more stock); METRIC alone +6.5 ± 0.8; P2 at equal stock +1.4 ± 0.8; P2 + METRIC +9.5 ± 0.8 (84.6 %); gain gone at 1.5× model error, -1.1 at 2×, -5.6 at 3× | VALIDATION §12-13 | Final (30 paired years per arm) |
| Fleet twin 30-day forecast inside its 95 % band in 3 of 3 held-out runs; availability off by 3.0, 1.6 and 3.4 pts (mean absolute 2.7; the mean signed error is -1.6, so do not print "within 1.6 pts") | `outputs/twin/twin_validation.json` | Final for these 3 runs |
| 72 tests, 14.6k lines | counted 2026-10-05 | Recount at build time |

## 8. How to frame the prediction result

The Phase 6 policy study is final (VALIDATION §12-13). Lead with the honest ranking, which is also the strongest
story for an expert judge: **stock reallocation is the big lever** (METRIC alone +6.5 pts, 11 % less stock; the Phase 5
check on other seeds gave +7.1); **prediction adds a smaller gain on top** (+1.4 at equal stock, +2.9 on the METRIC stock,
+9.5 for both); and **the models must stay within about 1.5× their measured error** for prediction to pay. The weekly
CP-SAT plan adds nothing measurable inside the simulator, so claim it only for plan accounting (640 vs 695 aircraft-days
down, 115 vs 126 inductions, surge shortfall 2 vs 14). Do not print the old "≈ +1.8 pts" figure.

## 9. Decisions and gaps before building

1. **Demo link.** Waiting for the team's Vercel link; both "click here" buttons (slide 6) use it. AirPower needs a backend as well as the built dashboard, so confirm what the Vercel page serves (static snapshot with baked data, or a hosted API) before screenshots are taken from it.
2. **Team ID** for slide 1.
3. **Phase 8** is done (roles, hash-chained audit log, TLS, Docker files). The container image itself was never run locally (no container engine; CI builds it), so say "Docker-ready", not "deployed".
4. **Sourced context statistics** for slide 5 and **sourced competitor cells** for slide 6 (§5, §6).
5. **Budget and revenue split.** The business model on slide 4 and any doughnut need real numbers from the team, or stay off the slide.
6. **Dashboard screenshots**: Readiness, Health map, a tail page (H2009 tail-rotor example from `DEMO.md`), Plan, Spares curve, What if. Capture after the store rebuild finishes, so the other session's run is not disturbed.
7. **Exact portal wording** of theme and title (slide 1).

## 10. Build order

1. **Do not start until the Vercel link is supplied.** Freeze numbers (§8).
2. Capture screenshots; export icons and logos (same icon set as AeroTwin's deck for visual unity).
3. Build slides 2 → 3 (the diagram-heavy ones), then 4, 5, 6, then 1.
4. Render every slide in PowerPoint and fix overflow; check links.
5. Run the plagiarism check; if over target, cut sentences down to fragments and swap tech-name chips for logos, as in AeroTwin v3.
