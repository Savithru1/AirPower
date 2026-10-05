"""P2 Predictive: the platform's predictions acting inside the simulated fleet (plan section 7).

What P2 knows (never the simulator's truth, except to degrade predictions on purpose):
* the HUMS records of the monitored positions, processed by the platform's own models:
  engines   - the A1 C-MAPSS trend model (age from the records), 90 % interval;
  batteries - the state-of-health trend (A4 / A5), interval from its leave-one-cell-out error;
  gearboxes - the per-serial baseline detector, thresholds and calibration fitted on a separate
              training fleet (A3 / A5); after an alarm the removal is planned within GEAR_NEED_FH
              and forced at GEAR_SAFE_FH (the P-F interval is 180-200 FH);
* life limits from the technical records (hours, landings, calendar);
* stock, backorders and the maintenance programme (what the stores and planning systems hold).

What P2 does
1. Spares ahead of removal: when a removal is predicted within the time a spare takes to come
   (a week if one is in stock anywhere, else the repair pipeline), the spare is drawn for that
   position (a kit). Kits queue behind aircraft already waiting for the part, and a held kit
   whose removal is more than ROB_DAYS away is handed to a waiting aircraft and drawn again.
2. Removal timing: a monitored unit found at an inspection flies on while the prediction allows
   and its kit is not yet in hand; with the kit in hand it is replaced at the next inspection, or
   in a short planned job if it would not last until then. A unit whose lower bound runs out is
   removed with or without the kit.
3. Rolling plan (planner="cpsat"): every PLAN_EVERY days the Phase 5 CP-SAT model plans the next
   HORIZON days from the simulated fleet's state: early inspections, planned removals bundled or
   inducted separately, the flying requirement including the known exercises.
   planner=None times removals with the rules of step 2 only (inspections start when due).
4. Aircraft with a pending removal and no kit fly last, which saves their remaining life.
5. Spares positioning (position=True): when a base has none of a part and the depot has one, the
   depot sends one to the base where the records predict most demand (removals per component
   hour from a separate training fleet's records, A6/A9, times the hours its aircraft fly). A
   demand at a base then finds the part there instead of waiting days for a transfer.

Stock is sized as for P0 (removal_life is inherited): P2 removes the same units at about the
same ages, so it is compared with P0 on the same inventory.

Confirmation before removal (as in practice): a HUMS-driven removal happens only after an inspection
of that position confirms the fault (a fault past its P point is found with probability P_CONFIRM);
a unit already found at a scheduled inspection counts as confirmed. When the targeted inspection finds
nothing, the aircraft has spent the diagnosis time on the ground, the unit stays fitted and the
alert is not acted on again for CLEAR_DAYS. So a false alarm costs a check; a missed fault flies on
until the next check, an inspection or a failure. A unit removed although healthy (possible through
the detection model of the planner's bundling) comes back from the shop no-fault-found (engine.py).

Prediction quality (value-of-prediction study): `quality` k scales each model's own error.
RUL models: RUL' = truth + k (RUL - truth), interval half-width x k (truth capped as in training).
Gearbox detector: HI' = HI + 0.5 (k - 1) x AR(1) noise per unit (k > 1); k = 0 alarms exactly in the
P-F window with the true remaining life. k = 1 is the models as built. `hums=False`: no HUMS
predictions at all (life limits from the records only).
"""
import json
import math
import time
from collections import deque

import numpy as np
from scipy.stats import norm, poisson

from airpower.config import OUTPUTS

from .policies import Policy

DAY = 24.0
KIT_BUFFER_DAYS = 7         # draw the kit this long before the predicted need, on top of the lead time
ROB_DAYS = 14               # a held kit needed later than this goes to an aircraft already waiting
GEAR_NEED_FH, GEAR_SAFE_FH = 100.0, 50.0
ENGINE_CAP_CYCLES = 125     # the engine model's target cap (engine_rul.CAP)
PLAN_EVERY, HORIZON = 7, 28
RATES_DB = "sqlite:///outputs/risk_s2.sqlite"      # training fleet's records (never an evaluation run)
FLY_ON_MIN_DAYS = 3         # a finding flies on only if the lower bound leaves at least this long
P_CONFIRM = 0.9             # a targeted inspection finds a fault that is past its P point
CLEAR_DAYS = 14             # an alert not confirmed is not acted on again for this long

_MODELS, _TABLES, _CAL = {}, {}, {}


def _engine_model(subset):
    if subset not in _MODELS:
        from airpower.health import artifacts
        from airpower.health.engine_rul import EngineRUL  # noqa: F401  (needed to unpickle)
        _MODELS[subset] = artifacts.load(OUTPUTS / "models" / f"engine_rul_{subset}.joblib")
    return _MODELS[subset]


def engine_table(bank, subset, unit, c0):
    """The A1 model's prediction at every cycle of a replayed trajectory whose HUMS history starts at
    cycle c0 (age known). The replay is exact, so the platform's HUMS processing would see these
    rows; cached per process and reused across runs."""
    key = (subset, unit, c0)
    if key not in _TABLES:
        import pandas as pd
        from data_prep import cmapss
        tr = bank.traj[subset][unit]
        cyc = np.arange(c0, len(tr))
        df = pd.DataFrame(tr[c0:], columns=cmapss.SETTINGS + cmapss.SENSORS)
        df.insert(0, "cycle", cyc + 1)
        df.insert(0, "unit", 0)
        df["cycles_seen"] = (cyc + 1).astype(float)
        P = _engine_model(subset).predict_frame(df)
        _TABLES[key] = (P["rul"].to_numpy(), P["lo"].to_numpy(), P["hi"].to_numpy())
    return _TABLES[key]


def _calibration():
    if not _CAL:
        p = OUTPUTS / "models" / "gearbox_detector.json"
        _CAL["gear"] = json.loads(p.read_text()) if p.exists() else None
        b = OUTPUTS / "health" / "battery_predictions.csv"
        if b.exists():
            import pandas as pd
            bp = pd.read_csv(b)
            _CAL["qlog"] = float(np.quantile(np.abs(np.log(np.maximum(bp["trend"], 1.0) / bp["true"])), 0.9))
        else:
            _CAL["qlog"] = 1.0
    return _CAL


def theil_sen(x, y):
    i, j = np.triu_indices(len(x), 1)
    dx = x[j] - x[i]
    ok = dx > 1e-9
    if not ok.any():
        return 0.0, float(np.median(y))
    sl = float(np.median((y[j] - y[i])[ok] / dx[ok]))
    return sl, float(np.median(y) - sl * np.median(x))


VIB = ["Vib_RMS", "Vib_Kurtosis", "crest", "env_spec_kurt"]


class _Gear:
    """Incremental version of fleet_risk.gearbox_z / gearbox_risk for one installed gearbox."""
    BASE_N, PERSIST, ALPHA = 25, 3, 0.1

    def __init__(self):
        self.base, self.n = [], 0
        self.mu = self.sd = None
        self.num = np.zeros(4)
        self.den = 0.0
        self.flags = deque(maxlen=self.PERSIST)
        self.hi, self.alarm, self.p = None, False, 0.0
        self.noise = 0.0
        self.fh_alarm = None

    def update(self, x, cal, sigma, rng):
        x = np.array([math.log(max(x[0], 1e-12)), x[1], x[2], x[3]])
        self.n += 1
        if self.mu is None:
            self.base.append(x)
            if len(self.base) == self.BASE_N:
                b = np.array(self.base)
                self.mu = b.mean(axis=0)
                self.sd = np.maximum(b.std(axis=0, ddof=1), np.maximum(0.05 * np.abs(self.mu), 1e-6))
            return
        z = (x - self.mu) / self.sd
        self.num = z + (1 - self.ALPHA) * self.num          # pandas ewm(adjust=True)
        self.den = 1.0 + (1 - self.ALPHA) * self.den
        s = self.num / self.den
        th = np.array([cal["thresholds"][c] for c in VIB])
        hi = float(np.max(np.abs(s) / th))
        if sigma > 0:                                       # degraded on purpose (value-of-prediction study)
            self.noise = 0.95 * self.noise + math.sqrt(1 - 0.95 ** 2) * float(rng.standard_normal())
            hi = max(0.0, hi + sigma * self.noise)
        self.hi = hi
        self.flags.append(hi >= 1.0)
        self.alarm = len(self.flags) == self.PERSIST and all(self.flags)
        c = cal["calibration"]
        self.p = 1 / (1 + math.exp(-(c["intercept"] + c["coef"] * hi)))


class Predictive(Policy):
    name = "P2"
    observes = True

    def __init__(self, quality=1.0, hums=True, planner="cpsat", plan_every=PLAN_EVERY, horizon=HORIZON,
                 time_limit=5.0, workers=2, risk_aware_flying=True, position=True, rates_db=RATES_DB):
        self.k = float(quality)
        self.hums = bool(hums)
        self.planner = planner
        self.plan_every, self.H = int(plan_every), int(horizon)
        self.time_limit, self.workers = float(time_limit), int(workers)
        self.risk_aware_flying = risk_aware_flying
        self.position, self.rates_db = bool(position), rates_db
        self.name = "P2"

    # ================================================================== set-up
    def start(self, sim):
        self.sim = sim
        self.rng = np.random.default_rng([sim.seed, 77])
        self.today = 0
        self.cal = _calibration()
        self.plans = {}                  # (tail, slot) -> plan dict
        self.inst = {}                   # installation key -> HUMS state (engine c0, battery buffer, gearbox)
        self.sched = {}                  # day -> [(kind, tail, payload)]
        self.stats = dict(kits_drawn=0, kits_used=0, kits_handed_over=0, findings_flown_on=0, planned_removals=0,
                          bundled_removals=0, forced_removals=0, early_inspections=0, plans_solved=0,
                          plans_failed=0, plan_seconds=0.0, positioned=0, checks_no_fault=0, confirmed=0, kits_released=0)
        self.pred_log = {"engine": [], "battery": [], "gearbox": []}
        self.look = {t.id: min(i.interval_fh or 1e9 for i in t.inspections) for t in sim.cfg.types.values()}
        self.rates = base_demand(sim, self.rates_db) if self.position else {}
        self.shipped = {}

    @staticmethod
    def _key(ac, slot, s):
        return (s.sid, s.renewals, ac.tail, slot)

    # ================================================================== HUMS processing
    def observe(self, sim, ac, slot, s, ch):
        if not self.hums:
            return
        key = self._key(ac, slot, s)
        st = self.inst.get(key)
        kind = s.spec.hums["kind"]
        if st is None:
            st = self.inst[key] = {"kind": kind}
            if kind == "cmapss":
                st["c0"] = int(s.age_fh / s.spec.hums["hours_per_cycle"])
            elif kind == "battery":
                st["buf"], st["new"], st["pred"] = deque(maxlen=30), 0, None
            elif kind == "vibration":
                st["gear"] = _Gear()
        if kind == "battery":
            st["buf"].append((s.age_fh, ch["soh"]))
            st["new"] += 1
        elif kind == "vibration" and self.cal["gear"] is not None and self.k > 0:
            g = st["gear"]
            g.update([ch[c] for c in VIB], self.cal["gear"], 0.5 * max(self.k - 1.0, 0.0), self.rng)
            if g.alarm and g.fh_alarm is None:
                g.fh_alarm = s.age_fh

    def _hums_prediction(self, sim, ac, slot, s):
        """(need_fh, safe_fh) from the HUMS models, or None when they see nothing to act on."""
        st = self.inst.get(self._key(ac, slot, s))
        if st is None:
            return None
        k, kind, meas = self.k, st["kind"], sim.env.now >= sim.warmup * DAY
        true = s.ttf - s.clock()                              # used only to degrade / perfect the prediction
        if kind == "cmapss":
            if not sim.bank.real:
                return None
            hpc = s.spec.hums["hours_per_cycle"]
            rul, lo, hi = engine_table(sim.bank, s.sig["subset"], s.sig["unit"], st["c0"])
            i = min(int(s.age_fh / hpc), s.sig["L"] - 1) - st["c0"]
            if i < 0:
                return None
            r, l = rul[i] * hpc, lo[i] * hpc
            cap = ENGINE_CAP_CYCLES * hpc
            tc = min(true, cap)
            r2, l2 = tc + k * (r - tc), tc + k * (r - tc) - k * (r - l)
            if meas and true <= cap:
                self.pred_log["engine"].append((true, r2, max(l2, 0.0)))
            if r2 >= 0.9 * cap:                                # the model sees a healthy engine
                return None
            return max(r2, 0.0), max(l2, 0.0)
        if kind == "battery":
            if st["new"] >= 3 or st["pred"] is None:
                b = np.array(st["buf"])
                if len(b) < 10 or np.ptp(b[:, 0]) < 1e-6:
                    return None
                sl, ic = theil_sen(b[:, 0], b[:, 1])
                level = ic + sl * b[-1, 0]
                rul = float(np.clip((s.spec.hums["eol_soh"] - level) / sl, 0, 5000)) if sl < 0 else 5000.0
                st["pred"], st["new"], st["at"] = rul, 0, s.age_fh
            rul = max(st["pred"] - (s.age_fh - st["at"]), 0.0)
            q = self.cal["qlog"]
            t1 = max(true, 1.0)
            r2 = t1 * math.exp(k * math.log(max(rul, 1.0) / t1))
            l2 = r2 * math.exp(-k * q)
            if meas and true <= 300:
                self.pred_log["battery"].append((true, r2, l2))
            if r2 > 1000:
                return None
            return r2, l2
        if kind == "vibration":
            degrading = s.clock() >= s.p_point
            if k == 0:
                return (true, true) if degrading else None
            g = st.get("gear")
            if g is None or g.hi is None:
                return None
            if meas:
                self.pred_log["gearbox"].append((degrading, g.hi, g.alarm))
            if g.alarm:
                since = s.age_fh - (g.fh_alarm or s.age_fh)
                return max(GEAR_NEED_FH - since, 0.0), max(GEAR_SAFE_FH - since, 0.0)
            if g.p >= 0.2:                                     # watch: draw a kit, no removal yet
                return 2 * GEAR_NEED_FH, math.inf
        return None

    def predict(self, sim, ac, slot, s):
        """dict(need_days, safe_days, source) for a position, or None."""
        u = max(sim.fh_per_day[ac.unit], 1e-6)
        best = None
        lim = s.spec.life
        if lim:
            look = self.look[ac.type.id]                       # removed at the inspection before the limit
            rem = []
            if lim.get("fh"):
                rem.append((lim["fh"] - s.age_fh - look) / u)
            if lim.get("cycles"):
                rem.append((lim["cycles"] - s.age_cyc - look * sim.cyc_per_fh[ac.unit]) / (u * sim.cyc_per_fh[ac.unit]))
            if lim.get("days"):
                rem.append(lim["days"] - (sim.env.now - s.cal_t0) / DAY)
            if rem:
                d = max(min(rem), 0.0)
                best = dict(need_days=d, safe_days=d, source="life")
        if self.hums and s.spec.hums:
            h = self._hums_prediction(sim, ac, slot, s)
            if h is not None:
                need, safe = h[0] / u, h[1] / u
                if best is None or need < best["need_days"]:
                    best = dict(need_days=need, safe_days=safe, source="hums")
        return best

    # ================================================================== kits
    def kit_lead(self, sim, ac, pn):
        st = sim.stores
        if st.stock[ac.base][pn]:
            return 1.0
        if any(st.stock[l][pn] for l in st.locs):
            return 4.0
        return st.sizing[pn]["pipeline_days"]

    def _draw(self, sim, ac, slot, plan):
        ev, _ = sim.stores.request(ac.base, plan["pn"], ac.tail, f"KIT-{ac.tail}-{slot}", kit=True)
        plan["kit"] = dict(ev=ev, rec=sim.log["demands"][-1], at=ac.base)
        self.stats["kits_drawn"] += 1

    @staticmethod
    def in_hand(plan):
        return plan.get("kit") is not None and plan["kit"]["ev"].triggered

    def take_kit(self, sim, ac, slot):
        plan = self.plans.get((ac.tail, slot))
        if plan is None or plan.get("kit") is None or plan["pn"] != ac.slots[slot].pn:
            return None
        kit = plan.pop("kit")
        plan["done"] = True
        self.stats["kits_used"] += 1
        return kit["ev"], kit["ev"].triggered

    def _hand_over(self, sim):
        """Held kits not needed soon go to aircraft already waiting for the part."""
        for (tail, slot), plan in self.plans.items():
            if plan.get("done") or not self.in_hand(plan) or plan["need_days"] <= ROB_DAYS:
                continue
            waiting = sim.stores.waiting(plan["pn"])
            if not waiting:
                continue
            rec, ev = waiting[0]
            kit = plan.pop("kit")
            sim.stores.hand_over(kit["ev"].value, kit["at"], rec, ev)
            self.stats["kits_handed_over"] += 1

    def _position(self, sim, day, min_month=0.1):
        """Depot stock to the empty base with the most predicted demand (one unit a day per part)."""
        st = sim.stores
        for pn, by_base in self.rates.items():
            if not st.stock[st.depot][pn]:
                continue
            empty = [b for b, r in by_base.items() if r * 30 >= min_month and not st.stock[b][pn]
                     and self.shipped.get((pn, b), -99) < day - 5]
            if not empty:
                continue
            b = max(empty, key=lambda x: by_base[x])
            s = st.stock[st.depot][pn].pop()
            sim.env.process(st._ship(s, b))
            self.shipped[(pn, b)] = day
            self.stats["positioned"] += 1

    def _confirm(self, sim, ac, slot, s, plan, day):
        """Is the predicted fault confirmed by an inspection of the position? (The inspection's outcome
        depends on the unit's physical state, as every inspection in the simulator does.)"""
        if plan.get("confirmed"):
            return True
        if s.clock() >= s.p_point and self.rng.random() < P_CONFIRM:
            plan["confirmed"] = True
            self.stats["confirmed"] += 1
            return True
        plan["cleared_until"] = day + CLEAR_DAYS
        self.stats["checks_no_fault"] += 1
        self._release(sim, plan)
        return False

    def _release(self, sim, plan):
        """Give back the spare drawn for an alert that a check cleared or that went away: a held unit
        returns to stock (or to an aircraft waiting for it), a pending backorder is cancelled."""
        kit = plan.get("kit")
        if kit is None:
            return
        st = sim.stores
        if kit["ev"].triggered:
            st._txn("receipt", kit["at"], kit["ev"].value)
            st.arrive(kit["ev"].value, kit["at"])
        else:
            q = st.backorders[plan["pn"]]
            for i, (rec, ev) in enumerate(q):
                if rec is kit["rec"]:
                    del q[i]
                    rec["cancelled"] = True
                    break
            else:
                return                                          # in transit: released when it arrives
        plan["kit"] = None
        self.stats["kits_released"] = self.stats.get("kits_released", 0) + 1

    def _remove(self, sim, ac, slot, s, plan, day, why, stat):
        """A HUMS-driven removal, after a confirming check (a separate short job if not confirmed)."""
        if plan.get("cleared_until", -1) > day:
            return
        if self._confirm(sim, ac, slot, s, plan, day):
            if sim.planned_removal(ac, slot, why=why):
                self.stats[stat] += 1
        else:
            sim.check_job(ac, slot, why=why)

    # ================================================================== daily decisions
    def daily(self, sim, day):
        if day is None:
            return
        self.today = day
        self._hand_over(sim)
        if self.position:
            self._position(sim, day)
        for ac in sim.aircraft:
            for slot, s in ac.slots.items():
                if s is None or not (s.spec.life or (self.hums and s.spec.hums)):
                    continue
                key = (ac.tail, slot)
                plan = self.plans.get(key)
                life = (s.sid, s.renewals)
                if plan is not None and (plan.get("done") or plan["life"] != life) and plan.get("kit") is None:
                    plan = self.plans.pop(key)
                    plan = None
                if slot in ac.open_slot:
                    continue
                pr = self.predict(sim, ac, slot, s)
                if pr is None:
                    if plan is not None and plan.get("source") == "hums" and not plan.get("confirmed"):
                        self._release(sim, plan)                 # the alert went away
                    continue
                if plan is None:
                    plan = self.plans[key] = dict(life=life, pn=s.pn, kit=None, tail=ac.tail, slot=slot)
                plan.update(pr)
                if plan.get("kit") is None and pr["need_days"] <= self.kit_lead(sim, ac, s.pn) + KIT_BUFFER_DAYS:
                    self._draw(sim, ac, slot, plan)
                if pr["source"] != "hums":
                    continue                                     # life limits: removed by the programme, kit in hand
                if pr["safe_days"] <= 0.5:
                    self._remove(sim, ac, slot, s, plan, day, "prediction lower bound reached", "forced_removals")
                elif self.planner is None and self.in_hand(plan) and not ac.in_inspection \
                        and self.days_to_inspection(sim, ac) > pr["safe_days"]:
                    self._remove(sim, ac, slot, s, plan, day, "spare in hand, would not last to the next inspection",
                                 "planned_removals")
        if self.planner == "cpsat":
            if day % self.plan_every == 0:
                self._replan(sim, day)
            self._execute(sim, day)

    def days_to_inspection(self, sim, ac):
        u = max(sim.fh_per_day[ac.unit], 1e-6)
        out = math.inf
        for i in ac.type.inspections:
            if i.interval_fh:
                out = min(out, (i.interval_fh - (ac.fh - ac.insp_fh[i.id])) / u)
            if i.interval_days:
                out = min(out, i.interval_days - (sim.env.now - ac.insp_t[i.id]) / DAY)
        return max(out, 0.0)

    def inspection_extra(self, sim, ac, insp, slot, s):
        plan = self.plans.get((ac.tail, slot))
        if plan is None or plan.get("done") or plan.get("source") != "hums" or plan["pn"] != s.pn:
            return None
        if self.in_hand(plan) and plan.get("cleared_until", -1) <= self.today:
            if self._confirm(sim, ac, slot, s, plan, self.today):      # the inspection in progress is the check
                self.stats["bundled_removals"] += 1
                return "predictive"
        return None

    def inspection_finding(self, sim, ac, insp, slot, s):
        if not (self.hums and s.spec.hums):
            return "inspection"
        plan = self.plans.get((ac.tail, slot))
        if plan is not None and self.in_hand(plan):
            return "inspection"
        pr = self.predict(sim, ac, slot, s)
        if pr is None or pr["source"] != "hums" or pr["safe_days"] < FLY_ON_MIN_DAYS:
            return "inspection"
        if plan is None or plan["life"] != (s.sid, s.renewals):
            plan = self.plans[(ac.tail, slot)] = dict(life=(s.sid, s.renewals), pn=s.pn, kit=None, tail=ac.tail, slot=slot)
        plan.update(pr)
        plan["confirmed"] = True                                       # the inspection found it
        if plan.get("kit") is None:
            self._draw(sim, ac, slot, plan)
        self.stats["findings_flown_on"] += 1
        return None

    def select(self, sim, unit, cands, n):
        if not self.risk_aware_flying:
            return super().select(sim, unit, cands, n)
        pending = {t for (t, _), p in self.plans.items()
                   if not p.get("done") and p.get("source") == "hums" and not self.in_hand(p) and p["safe_days"] < 30}
        return sorted(cands, key=lambda ac: (ac.tail in pending, ac.recent_fh))[:n]

    # ================================================================== rolling CP-SAT plan
    def _replan(self, sim, day):
        from airpower.plan.planner import optimise
        t0 = time.time()
        P = SimPlan(sim, self, day, self.H)
        starts, bundled, info = optimise(P, time_limit=self.time_limit, workers=self.workers) if P.jobs else ({}, {}, {})
        self.stats["plan_seconds"] += time.time() - t0
        self.sched, self.d_plan = {}, day
        if starts is None:
            self.stats["plans_failed"] += 1
            return
        self.stats["plans_solved"] += 1
        jobs = {j["id"]: j for j in P.jobs}
        for jid, s in starts.items():
            if s < self.plan_every:
                self.sched.setdefault(day + s, []).append(jobs[jid])
        for cid, iid in bundled.items():
            plan = self.plans.get(jobs[cid]["key"])
            if plan is not None:
                plan["bundle"] = jobs[iid]["name"]

    def _execute(self, sim, day):
        by_tail = {ac.tail: ac for ac in sim.aircraft}
        for j in self.sched.pop(day, []):
            ac = by_tail[j["tail"]]
            if j["kind"] == "inspection":
                # only deliberate early inductions: a start on the estimated due day is left to the
                # fleet, which starts the inspection when it is actually due
                if j["occ"] == 0 and day < self.d_plan + j["due"] - 1 and not ac.in_inspection and sim.due(ac) is None:
                    insp = next(i for i in ac.type.inspections if i.id == j["name"])
                    if sim.start_inspection(ac, insp):
                        self.stats["early_inspections"] += 1
            else:
                plan = self.plans.get(j["key"])
                slot = j["key"][1]
                if plan is not None and not plan.get("done") and self.in_hand(plan) and not ac.in_inspection \
                        and ac.slots.get(slot) is not None:
                    self._remove(sim, ac, slot, ac.slots[slot], plan, day, "planned induction", "planned_removals")

    # ================================================================== reporting
    def summary(self):
        out = dict(self.stats, quality=self.k, hums=self.hums, planner=self.planner)
        e = np.array(self.pred_log["engine"]) if self.pred_log["engine"] else None
        if e is not None:
            out["engine_rmse_fh_last125"] = float(np.sqrt(np.mean((e[:, 1] - e[:, 0]) ** 2)))
            out["engine_lower_bound_ok"] = float(np.mean(e[:, 2] <= e[:, 0]))
        b = np.array(self.pred_log["battery"]) if self.pred_log["battery"] else None
        if b is not None:
            out["battery_within_30pct_near_eol"] = float(np.mean(np.abs(b[:, 1] - b[:, 0]) <= 0.3 * np.maximum(b[:, 0], 1)))
        g = self.pred_log["gearbox"]
        if g:
            y = np.array([x[0] for x in g], bool)
            h = np.array([x[1] for x in g], float)
            if y.any() and (~y).any():
                from sklearn.metrics import roc_auc_score
                out["gearbox_auc_degrading"] = float(roc_auc_score(y, h))
        return out


def base_demand(sim, url):
    """Predicted removals per day of each part at each base: removals per component hour from a
    training fleet's records (reliability_part, A6/A9) x installed units x hours flown per day.
    Without that store, the simulator's own stock-sizing rates stand in (CI)."""
    rate = {}
    try:
        import pandas as pd
        from sqlalchemy import text
        from airpower.ingest.resolve import key
        from airpower.model import schema as S
        eng = S.engine(url)
        r = pd.read_sql(text("select pn, removals, component_hours from reliability_part"), eng)
        alias = pd.read_sql(text("select alias, pn from part_alias"), eng).set_index("alias")["pn"].to_dict()
        per_h = {p: x.removals / x.component_hours for p, x in zip(r["pn"], r.itertuples()) if x.component_hours > 0}
        for pn in sim.parts:
            plat = alias.get(key(pn), pn)
            if plat in per_h:
                rate[pn] = per_h[plat]
    except Exception:
        rate = {}
    out = {}
    for u in sim.cfg.units:
        t = sim.cfg.types[u["type"]]
        for p in t.positions:
            if p.pn in rate:
                r = rate[p.pn] * p.qty * u["aircraft"] * sim.fh_per_day[u["id"]]
            else:
                r = sim.stores.sizing[p.pn]["rate_per_day"] * p.qty * u["aircraft"] / max(
                    sum(x["aircraft"] * next(q.qty for q in sim.cfg.types[x["type"]].positions if q.pn == p.pn)
                        for x in sim.cfg.units if any(q.pn == p.pn for q in sim.cfg.types[x["type"]].positions)), 1)
            out.setdefault(p.pn, {})
            out[p.pn][u["base"]] = out[p.pn].get(u["base"], 0.0) + r
    return out


class SimPlan:
    """The Phase 5 planning problem (planner.Problem's interface) built from the simulated fleet's
    state instead of the store: inspections from the airframe counters, planned removals from P2's
    plans, work already in progress, the flying programme including known exercises."""

    soft_capacity = True

    def __init__(self, sim, pol, day, H):
        import pandas as pd
        from airpower.plan.planner import FLEX
        self.sim, self.d0, self.H = sim, day, H
        self.ac = pd.DataFrame([dict(tail=a.tail, unit_id=a.unit, base=a.base, type_id=a.type.id)
                                for a in sim.aircraft]).set_index("tail")
        self.n_unit = self.ac.groupby("unit_id").size().to_dict()
        self.blocked, self.grounded, self.jobs = {}, [], []
        now = sim.env.now
        self._capacity(sim, now)
        agency = lambda ac, lvl: {"O": sim.o_agency[ac.unit], "I": sim.i_agency[ac.base], "D": sim.d_agency}[lvl]  # noqa: E731
        for ac in sim.aircraft:
            u = max(sim.fh_per_day[ac.unit], 1e-6)
            if ac.state not in ("SERVICEABLE", "FLYING"):
                if ac.insp_now is not None:
                    insp, t_in = ac.insp_now
                    rem = self._dur(sim, ac, insp) - (now - t_in) / DAY
                    self.blocked[ac.tail] = (int(min(H, max(1, math.ceil(rem)))), "SCHEDULED")
                    ag = agency(ac, insp.level)
                    for d in range(int(min(H, max(1, math.ceil(rem))))):
                        self.used[(ag, d)] = self.used.get((ag, d), 0) + 1
                else:
                    self.blocked[ac.tail] = (7 if ac.state == "NMCS" else 1, ac.state)
            due = {}
            for i in ac.type.inspections:
                if ac.insp_now is not None and (i.interval_fh or 0) <= (ac.insp_now[0].interval_fh or 0):
                    continue                                   # reset when the inspection in progress closes
                c = []
                if i.interval_fh:
                    c.append((i.interval_fh - (ac.fh - ac.insp_fh[i.id])) / u)
                if i.interval_days:
                    c.append(i.interval_days - (now - ac.insp_t[i.id]) / DAY)
                due[i.id] = (max(0, int(math.floor(min(c)))), i)
            for iid, (d, i) in due.items():
                # a higher inspection due at about the same time does this one as well
                if any((j.interval_fh or 0) > (i.interval_fh or 0) and dj <= d + 2 for dj, j in due.values()):
                    continue
                ivl = min(([i.interval_fh / u] if i.interval_fh else []) + ([i.interval_days] if i.interval_days else []))
                dur = self._dur(sim, ac, i)
                flex = max(1, int(round(FLEX * ivl)))
                if d >= H:
                    continue
                if d == 0 and ac.insp_now is None:             # falls due today: the fleet starts it anyway
                    self.blocked[ac.tail] = (min(H, dur), "SCHEDULED")
                    for dd in range(min(H, dur)):
                        self.used[(agency(ac, i.level), dd)] = self.used.get((agency(ac, i.level), dd), 0) + 1
                    d += max(int(round(ivl)), dur + 1)
                    if d >= H:
                        continue
                    k0 = 0                                   # next one keeps its own due window
                else:
                    k0 = 0
                k = k0
                while d < H:
                    self.jobs.append(dict(id=f"{ac.tail}:{iid}:{k}", tail=ac.tail, kind="inspection", name=iid, occ=k,
                                          level=i.level, agency=agency(ac, i.level), dur=dur, due=d,
                                          interval=max(int(round(ivl)), dur + 1), flex=flex, overdue=False))
                    k += 1
                    d += max(int(round(ivl)), dur + 1)
        for (tail, slot), plan in pol.plans.items():
            if plan.get("done") or plan.get("source") != "hums" or plan.get("cleared_until", -1) > day:
                continue
            ac = next(a for a in sim.aircraft if a.tail == tail)
            if slot in ac.open_slot or ac.slots.get(slot) is None:
                continue
            spec = ac.slot_spec[slot]
            if pol.in_hand(plan):
                rel = 0
            elif plan.get("kit") is not None and plan["kit"]["rec"].get("src"):
                rel = 4                                         # in transit
            else:
                rel = H                                         # waiting on the pipeline: not plannable yet
            shift = sim.ag_spec[agency(ac, spec.level)]["shift_h"]
            self.jobs.append(dict(id=f"{tail}:{slot}:cond", key=(tail, slot), tail=tail, kind="condition",
                                  name=f"{slot} on condition", pn=plan["pn"], level=spec.level,
                                  agency=agency(ac, spec.level), dur=max(1, math.ceil(spec.mttr_h * 0.6 / shift)),
                                  deadline=int(max(0, math.floor(plan["safe_days"]))) if math.isfinite(plan["safe_days"]) else H,
                                  release=rel, weight=1.0))

    def _dur(self, sim, ac, insp):
        ag = {"O": sim.o_agency[ac.unit], "I": sim.i_agency[ac.base], "D": sim.d_agency}[insp.level]
        d = insp.work_h / sim.ag_spec[ag]["shift_h"] * 1.05
        return max(1, int(math.ceil(d + (2 * sim.dl["ferry_days"] if insp.level == "D" else 0))))

    def _capacity(self, sim, now, days=60):
        """Capacity for planned work: bays / crews less the average unscheduled load of the last 60 days."""
        t0 = now - days * DAY
        load = {}
        for w in reversed(sim.log["work_orders"]):
            if w["t_opened"] < t0 - 30 * DAY:
                break
            if w["kind"] != "unscheduled" or w["t_started"] is None:
                continue
            a, b = max(w["t_started"], t0), min(w["t_closed"] if w["t_closed"] is not None else now, now)
            if b > a:
                load[w["agency"]] = load.get(w["agency"], 0.0) + (b - a) / (days * DAY)
        self.cap = {a: max(1, int(spec["capacity"] - round(load.get(a, 0.0)))) for a, spec in sim.ag_spec.items()}
        self.used = {}

    def cap_day(self, a, d):
        return max(0, self.cap.get(a, 1) - self.used.get((a, d), 0))

    def required(self, unit, d):
        sim = self.sim
        day = self.d0 + d
        wd = (sim.start.weekday() + day) % 7
        factor = 1.0
        for s in sim.cfg.surges:
            if s["start_day"] <= day < s["start_day"] + s["days"]:
                factor = s["factor"]
        if not (wd in sim.cfg.flying_weekdays or (factor > 1.0 and wd == 5)):
            return 0
        u = next(x for x in sim.cfg.units if x["id"] == unit)
        f = u["flying"]
        return int(math.ceil(poisson.ppf(0.75, f["sorties_per_day"] * factor) / f["max_per_day"]))


__all__ = ["Predictive", "SimPlan", "engine_table", "norm"]
