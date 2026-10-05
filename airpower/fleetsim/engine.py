"""FleetSim engine: discrete-event simulation of an air fleet (SimPy).

Time is in hours since 00:00 local time on the configured start date.

What is simulated
* Flying: each flying day every unit flies its task in waves; serviceable aircraft with the
  fewest recent hours are chosen (natural hour balancing). Shortfalls are recorded.
* Components: every position on every aircraft holds a serial. A serial's time to failure is
  drawn from its position's Weibull law (in flight hours or landings) or, for engines and
  batteries, set by the real trajectory it replays (signals.py). A P point before failure
  marks when the defect becomes detectable at an inspection or in HUMS.
  4 % of rotable serials are "rogue": they keep failing early after every repair.
* Defects: in-service failures, no-fault-found removals, minor snags (some recurring),
  inspection findings, life expiries, preventive replacements. Some can be deferred.
* Maintenance: O-level crews per unit, I-level bays per base, D-level bays at the depot, all
  capacity-limited (PriorityResource). Work hours are spread over the agency's shift hours.
  Scheduled inspections by flight hours and / or calendar; replacements need a spare.
* Spares: base and depot stock, lateral transfers, backorders filled first-in first-out,
  rotables repaired at the depot shop (turnaround, condemnation, procurement), consumables
  replaced one-for-one from procurement.
* HUMS: one record per sortie for HUMS-fitted types (some downloads missing).

The run produces clean truth logs (self.log). exports.py writes them as four fragmented
source systems; truth.py as truth tables and KPIs.
"""
import itertools
import math
from collections import defaultdict, deque
from datetime import date

import numpy as np
import simpy

from . import text
from .signals import SignalBank

DAY = 24.0
OK, FLY, INSP, DEPOT, NMCM, NMCS = "SERVICEABLE", "FLYING", "SCHEDULED", "DEPOT", "NMCM", "NMCS"
AVAILABLE = (OK, FLY)
# open-item status -> aircraft state, in priority order
STATUS_ORDER = [("depot", DEPOT), ("insp", INSP), ("spares", NMCS), ("maint", NMCM), ("wait", NMCM)]


def _u(rng, lohi):
    return float(rng.uniform(lohi[0], lohi[1]))


class Serial:
    __slots__ = ("sid", "sn", "pn", "spec", "rogue", "age_fh", "age_cyc", "tsn_fh", "cal_t0", "ttf", "p_point",
                 "sig", "renewals", "failed")

    def clock(self):
        return self.age_cyc if self.spec.clock == "cycles" else self.age_fh


class Aircraft:
    def __init__(self, tail, msn, hums_unit, type_spec, unit, base):
        self.tail, self.msn, self.hums_unit = tail, msn, hums_unit
        self.type, self.unit, self.base = type_spec, unit, base
        self.slots, self.slot_spec = {}, {}
        self.fh = self.cycles = 0.0
        self.insp_fh, self.insp_t = {}, {}
        self.items = {}                 # open grounding items: id -> status
        self.open_slot = {}             # slot -> open defect (rectification or deferral in progress)
        self.deferred_snags = []
        self.chronic = []
        self.in_inspection = False
        self.insp_now = None            # (inspection, start time) while one is in progress
        self.flying = False
        self.sorties_today = 0
        self.recent_fh = 0.0
        self.recent_t = 0.0
        self.state, self.state_since = OK, 0.0


class FleetSim:
    def __init__(self, cfg, seed=1, policy=None, real_signals=True, days=None, warmup_days=0, state=None):
        from .policies import Reactive
        self.cfg, self.seed = cfg, seed
        self.warmup = int(warmup_days)               # KPIs are measured after the warm-up (truth.kpis)
        self.days = int(days or cfg.days) + self.warmup
        self.env = simpy.Environment()
        self.rng = np.random.default_rng([seed, 0])
        # crn_streams: one random stream per aircraft (sorties, defects, inspections, work times), one
        # for the flying task and one per part number in the stores, so that two policies run on the
        # same seed meet the same random events for longer (common random numbers). Off by default:
        # the single stream reproduces the Phase 1-5 runs exactly.
        self.crn = bool(cfg.raw.get("crn_streams", False))
        self.rng_task = np.random.default_rng([seed, 6]) if self.crn else self.rng
        self.log_hums = bool(cfg.raw.get("log_hums", True))
        self.policy = policy or Reactive()
        self.bank = SignalBank(real_signals)
        self.log = defaultdict(list)
        self._ids = defaultdict(itertools.count)
        self.start = date.fromisoformat(str(cfg.start_date))
        self.dl = cfg.delays
        self.parts = cfg.parts()
        self.serials = []
        self._sn = defaultdict(lambda: 100)
        self._agencies()
        self._usage()
        self.mean_life = {pn: self._mean_life(specs[0]) for pn, specs in self.parts.items()}
        self._fleet()
        if state:
            self._apply_state(state)
        from .stores import Stores
        self.stores = Stores(self)
        if state:
            self._apply_open_items(state)
        self.policy.start(self)
        self.env.process(self._days())

    # ================================================================== set-up
    def nid(self, prefix):
        return f"{prefix}{next(self._ids[prefix]) + 1:06d}"

    def _agencies(self):
        self.agency, self.ag_spec = {}, {}
        self.o_agency, self.i_agency = {}, {}
        for a in self.cfg.agencies:
            self.agency[a["id"]] = simpy.PriorityResource(self.env, capacity=a["capacity"])
            self.ag_spec[a["id"]] = a
            if a["level"] == "O":
                for u in a["units"]:
                    self.o_agency[u] = a["id"]
            elif a["level"] == "I":
                self.i_agency[a["base"]] = a["id"]
            elif a["level"] == "D":
                self.d_agency = a["id"]
            elif a["level"] == "S":
                self.shop = self.agency[a["id"]]
                self.shop_id = a["id"]

    def _usage(self):
        """Expected flight hours and landings per aircraft-day, per unit (for planning look-aheads)."""
        n_fly = sum(1 for d in range(365) if (self.start.weekday() + d) % 7 in self.cfg.flying_weekdays)
        self.fh_per_day, self.cyc_per_fh = {}, {}
        for u in self.cfg.units:
            f = u["flying"]
            mean_h = 0.5 * (f["sortie_h"][0] + f["sortie_h"][1])
            self.fh_per_day[u["id"]] = f["sorties_per_day"] * mean_h * n_fly / 365.0 / u["aircraft"]
            self.cyc_per_fh[u["id"]] = (1.0 + f["landings_extra"]) / mean_h

    def _mean_life(self, spec):
        """Mean life in the position's clock unit (for preventive intervals and stock sizing)."""
        if spec.hums and spec.hums["kind"] in ("cmapss", "battery"):
            r = np.random.default_rng([self.seed, 99])
            return float(np.mean([self.bank.life(spec.hums, self.bank.assign(spec.hums, r)) for _ in range(200)]))
        if spec.weibull:
            return spec.weibull_mean()
        lim = spec.life.get(spec.clock)
        return float(lim) if lim else math.inf

    def _fleet(self):
        self.aircraft, self.by_unit = [], defaultdict(list)
        r = np.random.default_rng([self.seed, 2])
        for u in self.cfg.units:
            t = self.cfg.types[u["type"]]
            for k in range(u["aircraft"]):
                ac = Aircraft(tail=f"{t.id}{u['tail_start'] + k}", msn=str(u["msn_start"] + k),
                              hums_unit=f"HU-{5000 + u['msn_start'] % 1000 + k}" if t.id in self.cfg.hums["types"] else None,
                              type_spec=t, unit=u["id"], base=u["base"])
                ac.rng = np.random.default_rng([self.seed, 4, len(self.aircraft)]) if self.crn else self.rng
                ac.fh = float(r.uniform(100, 2000))
                ac.cycles = ac.fh * self.cyc_per_fh[u["id"]]
                for i in t.inspections:
                    ac.insp_fh[i.id] = ac.fh - (r.uniform(0, i.interval_fh) if i.interval_fh else 0.0)
                    ac.insp_t[i.id] = -(r.uniform(0, i.interval_days) * DAY if i.interval_days else 0.0)
                for p in t.positions:
                    for q in range(p.qty):
                        slot = p.pos if p.qty == 1 else f"{p.pos}#{q + 1}"
                        ac.slots[slot] = self.new_serial(p, init=True, unit=u["id"])
                        ac.slot_spec[slot] = p
                self.aircraft.append(ac)
                self.by_unit[u["id"]].append(ac)
                self._state_log(ac, OK)

    # ================================================================== serials
    def new_serial(self, spec, init=False, unit=None):
        s = Serial()
        s.sid = len(self.serials)
        self.serials.append(s)
        prefix = spec.pn.split("-")[0]
        self._sn[prefix] += 1
        s.sn = f"{prefix}{self._sn[prefix]:05d}"
        s.pn, s.spec = spec.pn, spec
        r = np.random.default_rng([self.seed, 1, s.sid])
        s.rogue = bool(spec.rotable and spec.weibull and r.random() < self.cfg.stores["rogue_fraction"])
        s.renewals = 0
        s.tsn_fh = 0.0
        self._sample_life(s, init=init, unit=unit)
        return s

    def renew(self, s):
        """Repair / overhaul returns the unit as new (its rogue defect stays)."""
        s.renewals += 1
        self._sample_life(s)

    def _sample_life(self, s, init=False, unit=None):
        spec = s.spec
        r = np.random.default_rng([self.seed, 1, s.sid, s.renewals])
        s.age_fh = s.age_cyc = 0.0
        s.failed = False
        s.sig = None
        s.cal_t0 = self.env.now
        lim = spec.life.get(spec.clock, math.inf)
        if spec.hums and spec.hums["kind"] in ("cmapss", "battery"):
            s.sig = self.bank.assign(spec.hums, r)
            s.ttf = self.bank.life(spec.hums, s.sig)
            s.p_point = self.bank.p_point(spec.hums, s.sig, s.ttf, spec.pf)
            age0 = r.uniform(0, 0.95) * min(s.ttf, lim) if init else 0.0
        elif spec.weibull:
            b, eta = spec.weibull
            if s.rogue:
                eta *= self.cfg.stores["rogue_factor"]
            age0 = self._equilibrium_age(b, eta, lim, r) if init else 0.0
            s.ttf = eta * ((age0 / eta) ** b - math.log(r.random())) ** (1.0 / b)
            s.p_point = s.ttf - spec.pf if spec.pf > 0 else s.ttf
            if spec.hums:
                s.sig = self.bank.assign(spec.hums, r)
        else:
            s.ttf = s.p_point = math.inf
            age0 = r.uniform(0, 0.9 * lim) if init and math.isfinite(lim) else 0.0
        if init:
            age0 = self._screened_age(s, age0, unit, r)
        if spec.clock == "cycles":
            s.age_cyc = age0
            s.age_fh = age0 / max(self.cyc_per_fh.get(unit, 1.0), 1e-9) if init else 0.0
        else:
            s.age_fh = age0
            s.age_cyc = age0 * self.cyc_per_fh.get(unit, 1.0) if init else 0.0
        if init:
            s.tsn_fh = s.age_fh + r.uniform(0, 3) * s.age_fh
            if spec.life.get("days"):
                s.cal_t0 = -r.uniform(0, 0.9 * spec.life["days"]) * DAY

    def _screened_age(self, s, age0, unit, r):
        """A unit that is already inside its P-F window on day 1 has, in a running fleet, been
        through the inspections since its P point. It is still fitted only if they all missed it;
        otherwise it was replaced then, and the fitted unit is younger. Without this, day 1 holds
        far too many detectable defects and the first inspections produce a burst of findings."""
        spec = s.spec
        insp = min(self.cfg.types[spec.type_id].inspections, key=lambda i: i.interval_fh or 1e9)
        step = (insp.interval_fh or 1e9) * (self.cyc_per_fh.get(unit, 1.0) if spec.clock == "cycles" else 1.0)
        p = spec.detect if spec.detect is not None else insp.detect_p
        for _ in range(5):
            if not (s.p_point <= age0 < s.ttf):
                return age0
            k = int((age0 - s.p_point) / step)
            if r.random() < (1.0 - p) ** k:
                return age0                                          # missed so far: still fitted
            since = age0 - s.p_point                                 # replaced at detection, this long ago
            age0 = r.uniform(0, since)
            if spec.weibull and not (spec.hums and spec.hums["kind"] in ("cmapss", "battery")):
                b, eta = spec.weibull
                if s.rogue:
                    eta *= self.cfg.stores["rogue_factor"]
                s.ttf = eta * ((age0 / eta) ** b - math.log(r.random())) ** (1.0 / b)
                s.p_point = s.ttf - spec.pf if spec.pf > 0 else s.ttf
        return min(age0, s.p_point) if s.p_point <= age0 else age0

    # ================================================================== start from a recorded state (twin)
    def _apply_state(self, st):
        """Airframe hours, inspection counters and component ages from the integrated records
        (airpower/twin/whatif.py). Lives are drawn again conditional on the recorded age."""
        by_tail = {a.tail: a for a in self.aircraft}
        for tail, a in st.get("aircraft", {}).items():
            ac = by_tail.get(tail)
            if ac is None:
                continue
            ac.fh = float(a["fh"])
            ac.cycles = ac.fh * self.cyc_per_fh[ac.unit]
            for iid, (fh_since, days_since) in a.get("inspections", {}).items():
                if iid in ac.insp_fh:
                    ac.insp_fh[iid] = ac.fh - float(fh_since)
                    ac.insp_t[iid] = -float(days_since) * DAY
            for slot, c in a.get("slots", {}).items():
                s = ac.slots.get(slot)
                if s is not None and c.get("age_fh") is not None:
                    self._set_age(s, float(c["age_fh"]), c.get("cal_days"), ac)

    def _set_age(self, s, age_fh, cal_days, ac):
        spec = s.spec
        r = np.random.default_rng([self.seed, 5, s.sid])
        cpf = self.cyc_per_fh.get(ac.unit, 1.0)
        age = age_fh * cpf if spec.clock == "cycles" else age_fh
        # the unit has survived to this age and has not been found at the inspections since its P
        # point: rejection sampling on both (as _screened_age does for a fleet started from scratch)
        if s.rogue and spec.weibull:
            # a unit that has lasted this long is unlikely to be a rogue: posterior probability
            b, eta = spec.weibull
            f, q = self.cfg.stores["rogue_factor"], self.cfg.stores["rogue_fraction"]
            lr = q * math.exp(-((age / (f * eta)) ** b))
            s.rogue = bool(r.random() < lr / (lr + (1 - q) * math.exp(-((age / eta) ** b))))
        insp = min(self.cfg.types[spec.type_id].inspections, key=lambda i: i.interval_fh or 1e9)
        per = cpf if spec.clock == "cycles" else 1.0
        step = (insp.interval_fh or 1e9) * per
        p_det = spec.detect if spec.detect is not None else insp.detect_p
        age_last = age - max(ac.fh - ac.insp_fh.get(insp.id, ac.fh), 0.0) * per     # unit age at the last inspection
        for _ in range(30):
            if spec.hums and spec.hums["kind"] in ("cmapss", "battery"):
                for _ in range(50):                      # a trajectory that has not ended at this age
                    sig = self.bank.assign(spec.hums, r)
                    if self.bank.life(spec.hums, sig) > age_fh:
                        break
                life = self.bank.life(spec.hums, sig)
                # older than every trajectory: at the end of its life (replay holds the last record)
                s.sig, s.ttf = sig, life if life > age_fh else age_fh + max(1.0, 0.05 * life)
                s.p_point = self.bank.p_point(spec.hums, sig, s.ttf, spec.pf)
            elif spec.weibull:
                b, eta = spec.weibull
                eta = eta * self.cfg.stores["rogue_factor"] if s.rogue else eta
                s.ttf = eta * ((age / eta) ** b - math.log(r.random())) ** (1.0 / b)
                s.p_point = s.ttf - spec.pf if spec.pf > 0 else s.ttf
            else:
                break
            k = int((age_last - s.p_point) // step) + 1 if s.p_point <= age_last else 0    # inspections it was missed at
            if r.random() < (1.0 - p_det) ** k:
                break
        s.age_fh, s.age_cyc, s.failed = age_fh, age_fh * cpf, False
        if cal_days is not None and spec.life.get("days"):
            s.cal_t0 = -float(cal_days) * DAY

    def _apply_open_items(self, st):
        """Aircraft already down when the records end: in an inspection (for its remaining expected
        time), or waiting for parts (the part is removed and demanded at time 0)."""
        by_tail = {a.tail: a for a in self.aircraft}
        for o in st.get("open", []):
            ac = by_tail.get(o["tail"])
            if ac is None:
                continue
            if o["kind"] == "inspection":
                insp = next((i for i in ac.type.inspections if i.id == o["inspection"]), None)
                if insp is not None and not ac.in_inspection:
                    ac.in_inspection = True
                    self.env.process(self._hold_inspection(ac, insp, float(o["remaining_days"])))
            elif o["kind"] == "spares":
                slot = next((sl for sl, s in ac.slots.items() if s is not None and s.pn == o["pn"]
                             and sl not in ac.open_slot), None)
                if slot is not None:
                    self.raise_defect(ac, slot, "failure", raised_by="records")

    def _hold_inspection(self, ac, insp, days):
        item = self.nid("IN")
        ac.items[item] = "depot" if insp.level == "D" else "insp"
        ac.insp_now = (insp, self.env.now - max(0.0, self.insp_days(ac, insp) - days) * DAY)
        self.set_state(ac)
        yield self.env.timeout(max(days, 0.1) * DAY)
        for i2 in ac.type.inspections:
            if (i2.interval_fh or 0) <= (insp.interval_fh or 0):
                ac.insp_fh[i2.id] = ac.fh
                ac.insp_t[i2.id] = self.env.now
        ac.items.pop(item, None)
        ac.in_inspection = False
        ac.insp_now = None
        self.set_state(ac)
        self.check_due(ac)

    def insp_days(self, ac, insp):
        """Expected calendar days of an inspection (work over the agency's shift, ferry for depot work)."""
        ag = {"O": self.o_agency[ac.unit], "I": self.i_agency[ac.base], "D": self.d_agency}[insp.level]
        return insp.work_h / self.ag_spec[ag]["shift_h"] + (2 * self.dl["ferry_days"] if insp.level == "D" else 0)

    @staticmethod
    def _equilibrium_age(b, eta, lim, r):
        """Age of a unit in a long-running fleet: density proportional to its survival function
        (renewal theory), truncated at a hard-time limit. A uniform age would put too many
        wear-out items near the end of their life on day 1 and cause a start-up burst."""
        top = min(3.0 * eta, lim)
        while True:
            a = r.uniform(0, top)
            if r.random() < math.exp(-((a / eta) ** b)):
                return a

    # ================================================================== state accounting
    def _state_log(self, ac, state):
        self.log["states"].append(dict(tail=ac.tail, unit=ac.unit, t=self.env.now, state=state))

    def set_state(self, ac):
        if ac.flying:
            st = FLY
        else:
            st = OK
            vals = set(ac.items.values())
            for key, s in STATUS_ORDER:
                if key in vals:
                    st = s
                    break
        if st != ac.state:
            ac.state, ac.state_since = st, self.env.now
            self._state_log(ac, st)

    def cal(self, agency_id, work_h, rng=None):
        """Calendar hours for `work_h` of work at an agency (spread over its shift hours)."""
        r = rng or self.rng
        return work_h * DAY / self.ag_spec[agency_id]["shift_h"] * float(r.lognormal(0.0, 0.25))

    # ================================================================== calendar
    def _days(self):
        f = self.cfg.flying_weekdays
        for d in range(self.days):
            t0 = d * DAY
            if t0 > self.env.now:
                yield self.env.timeout(t0 - self.env.now)
            wd = (self.start.weekday() + d) % 7
            for ac in self.aircraft:
                ac.sorties_today = 0
            self.env.process(self._at(t0 + 6.0, self._daily_checks, d))
            self.env.process(self._at(t0 + 8.0, self._snapshot, d))
            if wd == 0:
                self.env.process(self._at(t0 + 0.5, self.stores.snapshot, d))
            factor = 1.0
            for s in self.cfg.surges:
                if s["start_day"] <= d < s["start_day"] + s["days"]:
                    factor = s["factor"]
            if wd in f or (factor > 1.0 and wd == 5):
                for u in self.cfg.units:
                    fl = u["flying"]
                    n = self.rng_task.poisson(fl["sorties_per_day"] * factor)
                    waves = fl["waves_h"]
                    per = np.full(len(waves), n // len(waves))
                    per[: n % len(waves)] += 1
                    for w, k in zip(waves, per):
                        if k:
                            self.env.process(self._wave(u, d, t0 + w, int(k)))
        yield self.env.timeout(self.days * DAY - self.env.now)

    def _at(self, t, fn, *args):
        if t > self.env.now:
            yield self.env.timeout(t - self.env.now)
        fn(*args)

    def _snapshot(self, d):
        for u in self.cfg.units:
            acs = self.by_unit[u["id"]]
            c = defaultdict(int)
            for ac in acs:
                c[ac.state] += 1
            self.log["daily"].append(dict(day=d, unit=u["id"], type=u["type"], aircraft=len(acs),
                                          serviceable=c[OK] + c[FLY], nmcs=c[NMCS], nmcm=c[NMCM],
                                          scheduled=c[INSP] + c[DEPOT]))

    def _daily_checks(self, d=None):
        self.policy.daily(self, d)                     # a policy's own planning (P2), before anything falls due
        for ac in self.aircraft:
            self.check_due(ac)
            for slot, s in ac.slots.items():
                if s is None or slot in ac.open_slot:
                    continue
                days = s.spec.life.get("days")
                if days and (self.env.now - s.cal_t0) / DAY >= days:
                    self.raise_defect(ac, slot, "life", raised_by="records")

    # ================================================================== flying
    def _wave(self, u, d, t, n):
        if t > self.env.now:
            yield self.env.timeout(t - self.env.now)
        fl = u["flying"]
        cands = [ac for ac in self.by_unit[u["id"]]
                 if ac.state == OK and ac.sorties_today < fl["max_per_day"] and not self.due(ac)]
        chosen = self.policy.select(self, u, cands, n)
        self.log["tasking"].append(dict(day=d, unit=u["id"], t=t, tasked=n, flown=len(chosen)))
        for ac in chosen:
            ac.sorties_today += 1
            self.env.process(self._sortie(ac, _u(ac.rng, fl["sortie_h"]), 1 + int(ac.rng.poisson(fl["landings_extra"]))))

    def _sortie(self, ac, dur, landings):
        sid = self.nid("S")
        ac.flying = True
        self.set_state(ac)
        t0 = self.env.now
        first = math.inf
        for slot, s in ac.slots.items():
            if s is None or s.failed:
                continue
            inc = landings if s.spec.clock == "cycles" else dur
            if s.clock() + inc >= s.ttf:
                first = min(first, max(0.0, (s.ttf - s.clock()) / inc))
        aborted = math.isfinite(first) and ac.rng.random() < 0.4
        flown = max(0.1, first * dur) if aborted else dur
        if aborted:
            landings = 1
        yield self.env.timeout(flown)
        for s in ac.slots.values():
            if s is None:
                continue
            s.age_fh += flown
            s.tsn_fh += flown
            s.age_cyc += landings
        ac.fh += flown
        ac.cycles += landings
        ac.recent_fh = ac.recent_fh * math.exp(-(self.env.now - ac.recent_t) / (30 * DAY)) + flown
        ac.recent_t = self.env.now
        self.log["sorties"].append(dict(id=sid, tail=ac.tail, unit=ac.unit, t_takeoff=t0, t_landing=self.env.now,
                                        fh=flown, landings=landings, aborted=aborted))
        self._hums(ac, sid, t0)
        ac.flying = False
        for slot, s in list(ac.slots.items()):
            if s is not None and not s.failed and s.clock() >= s.ttf:
                s.failed = True
                self.log["failures"].append(dict(t=self.env.now, tail=ac.tail, slot=slot, sn=s.sn, pn=s.pn,
                                                 age=s.clock(), ttf=s.ttf, rogue=s.rogue, sortie=sid))
                self.raise_defect(ac, slot, "failure", raised_by="aircrew" if ac.rng.random() < 0.6 else "groundcrew",
                                  sortie=sid)
        self._random_defects(ac, flown, landings, sid)
        self.check_due(ac)
        self.set_state(ac)

    def _hums(self, ac, sid, t0):
        if ac.hums_unit is None:
            return
        if ac.rng.random() < self.cfg.hums["missing_download_p"]:
            self.log["hums_missing"].append(dict(sortie=sid, tail=ac.tail))
            return
        ch, sn, true = {}, {}, {}
        for slot, s in ac.slots.items():
            if s is not None and s.spec.hums:
                ch[slot] = self.bank.emit(s.spec.hums, s.sig, s.clock(), s.ttf, s.p_point, ac.rng,
                                          self.cfg.hums["noise"])
                sn[slot] = s.sn
                if self.policy.observes:                # what the policy's own HUMS processing receives
                    self.policy.observe(self, ac, slot, s, ch[slot])
                # truth only (never exported): for scoring the health models in the fleet
                true[slot] = (s.clock(), s.ttf, s.p_point, s.sig.get("label") if s.sig else None)
        if self.log_hums:                           # off in policy studies: the records are large and not needed
            self.log["hums"].append(dict(sortie=sid, tail=ac.tail, t_takeoff=t0, t_landing=self.env.now,
                                         channels=ch, serials=sn, truth=true))

    def _random_defects(self, ac, fh, landings, sid):
        sn = ac.type.snags
        for _ in range(ac.rng.poisson(sn["rate_per_fh"] * fh)):
            ata, txt = text.SNAGS[ac.rng.integers(len(text.SNAGS))]
            self.raise_snag(ac, ata, txt, sid)
            if ac.rng.random() < sn["recur_p"]:
                ac.chronic.append(dict(ata=ata, text=txt, left=int(ac.rng.geometric(0.3)), times=int(ac.rng.integers(1, 4))))
        for c in list(ac.chronic):
            c["left"] -= 1
            if c["left"] <= 0:
                self.raise_snag(ac, c["ata"], c["text"], sid, repeat=True)
                c["times"] -= 1
                c["left"] = int(ac.rng.geometric(0.3))
                if c["times"] <= 0:
                    ac.chronic.remove(c)
        for slot, s in ac.slots.items():
            if s is None:
                continue
            nff = s.spec.nff
            if nff <= 0 or slot in ac.open_slot:
                continue
            inc = landings if s.spec.clock == "cycles" else fh
            lam = nff / (1.0 - nff) / self.mean_life[s.pn] * inc
            if ac.rng.random() < lam:
                self.raise_defect(ac, slot, "nff", raised_by="aircrew", sortie=sid)

    def due(self, ac):
        """The highest inspection that is due, or None."""
        best = None
        for i in ac.type.inspections:
            d_fh = i.interval_fh and ac.fh - ac.insp_fh[i.id] >= i.interval_fh
            d_t = i.interval_days and self.env.now - ac.insp_t[i.id] >= i.interval_days * DAY
            if (d_fh or d_t) and (best is None or (i.interval_fh or 0) > (best.interval_fh or 0)):
                best = i
        return best

    def check_due(self, ac):
        if ac.in_inspection or ac.flying:
            return
        i = self.due(ac)
        if i is not None:
            ac.in_inspection = True
            self.env.process(self._inspection(ac, i))

    # ================================================================== defects
    def _defect(self, ac, kind, raised_by, slot=None, ata=None, txt=None, sortie=None, grounding=True, **extra):
        spec = ac.slot_spec.get(slot) if slot else None
        d = dict(id=self.nid("D"), tail=ac.tail, unit=ac.unit, base=ac.base, t_raised=self.env.now,
                 raised_by=raised_by, kind=kind, slot=slot, pos=spec.pos if spec else None,
                 pn=spec.pn if spec else None, ata=ata if ata is not None else (spec.ata if spec else None),
                 text=txt, grounding=grounding, deferred=False, t_closed=None, work_h=0.0,
                 removed_sn=None, installed_sn=None, wo=None, sortie=sortie, rect=None, **extra)
        self.log["defects"].append(d)
        return d

    def raise_defect(self, ac, slot, kind, raised_by, sortie=None, inspection=None):
        if slot in ac.open_slot:
            return None
        spec = ac.slot_spec[slot]
        if kind == "life":
            txt = f"{spec.name.lower()} life due"
        else:
            txt = text.symptom(spec.pos, ac.rng)
        defer = kind in ("failure", "nff") and ac.rng.random() < spec.defer
        d = self._defect(ac, kind, raised_by, slot=slot, txt=txt, sortie=sortie, grounding=not defer)
        ac.open_slot[slot] = d
        if defer:
            d["deferred"] = True
            self.env.process(self._deferred(ac, d, _u(ac.rng, (1, 7)) * DAY))
        else:
            self.env.process(self._rectify(ac, d))
        return d

    def raise_snag(self, ac, ata, txt, sortie, repeat=False):
        grounding = ac.rng.random() < ac.type.snags["grounding_p"]
        d = self._defect(ac, "snag", "aircrew" if ac.rng.random() < 0.7 else "groundcrew", ata=ata, txt=txt,
                         sortie=sortie, grounding=grounding, repeat=repeat)
        d["work_h"] = _u(ac.rng, ac.type.snags["work_h"])
        if grounding:
            self.env.process(self._rectify(ac, d))
        else:
            d["deferred"] = True
            ac.deferred_snags.append(d)
            self.env.process(self._deferred(ac, d, _u(ac.rng, (1, 5)) * DAY))

    def _deferred(self, ac, d, delay):
        yield self.env.timeout(delay)
        if d["t_closed"] is not None or d.get("_in_work"):
            return
        if d in ac.deferred_snags:
            ac.deferred_snags.remove(d)
        while ac.flying:                            # never start work on an aircraft in the air
            yield self.env.timeout(0.5)
        d["grounding"] = True
        d["_in_work"] = True                # an inspection must not take it over now
        yield from self._rectify(ac, d)

    def _open_wo(self, agency_id, ac, kind, defects=(), inspection=None):
        wo = dict(id=self.nid("WO"), agency=agency_id, tail=ac.tail, kind=kind, t_opened=self.env.now,
                  t_started=None, t_closed=None, defects=list(defects), inspection=inspection, labour_h=0.0)
        self.log["work_orders"].append(wo)
        return wo

    def _rectify(self, ac, d):
        item = d["id"]
        ac.items[item] = "wait"
        self.set_state(ac)
        spec = ac.slot_spec.get(d["slot"]) if d["slot"] else None
        level = spec.level if spec else "O"
        ag = self.o_agency[ac.unit] if level == "O" else self.i_agency[ac.base]
        wo = self._open_wo(ag, ac, "unscheduled", [item])
        d["wo"] = wo["id"]
        tf = self.dl["troubleshoot_frac"]
        work = (spec.mttr_h * tf) if spec else d["work_h"]
        with self.agency[ag].request(priority=1) as req:
            yield req
            wo["t_started"] = self.env.now
            ac.items[item] = "maint"
            self.set_state(ac)
            yield self.env.timeout(self.cal(ag, work, ac.rng))
        wo["labour_h"] += work
        if spec is not None:
            yield from self._replace(ac, [(d["slot"], d)], item, ag, wo, priority=1)
            d["work_h"] = spec.mttr_h
            ac.open_slot.pop(d["slot"], None)
        else:
            d["rect"] = text.RECT["snag"]
        yield self.env.timeout(_u(ac.rng, self.dl["admin_h"]))
        d["t_closed"] = wo["t_closed"] = self.env.now
        ac.items.pop(item, None)
        self.set_state(ac)

    def _replace(self, ac, jobs, item, ag, wo, priority, loc=None, status="maint", insp_name=None):
        """Remove the serials in `jobs` [(slot, defect)], wait for spares, install them."""
        loc = loc or ac.base
        evs, all_now = [], True
        for slot, d in jobs:
            old = ac.slots[slot]
            kit = self.policy.take_kit(self, ac, slot)          # spare already drawn for this position (P2)
            ev, now = kit if kit is not None else self.stores.request(loc, old.pn, ac.tail, wo["id"])
            evs.append((slot, d, old, ev))
            all_now &= now
            # a unit removed on a prediction before its fault exists comes back from the shop no-fault-found
            nff = d["kind"] == "nff" or (d["kind"] == "predictive" and old.clock() < old.p_point)
            d["nff_at_shop"] = nff
            self.stores.unserviceable(old, loc, nff=nff, tail=ac.tail, wo=wo["id"])
            ac.slots[slot] = None                      # the unit leaves the aircraft; the position is empty
        if not all_now:
            ac.items[item] = "spares"
            self.set_state(ac)
        got = yield simpy.AllOf(self.env, [e for *_, e in evs])
        ac.items[item] = "wait" if status == "maint" else status
        self.set_state(ac)
        work = sum(ac.slot_spec[slot].mttr_h for slot, *_ in evs) * (1 - self.dl["troubleshoot_frac"] if status == "maint" else 1)
        with self.agency[ag].request(priority=priority) as req:
            yield req
            ac.items[item] = status
            self.set_state(ac)
            yield self.env.timeout(self.cal(ag, work, ac.rng))
        wo["labour_h"] += work
        for slot, d, old, ev in evs:
            new = got[ev]
            ac.slots[slot] = new
            spec = ac.slot_spec[slot]
            d["removed_sn"], d["installed_sn"] = old.sn, new.sn
            key = {"failure": "replace", "nff": "replace_nff", "predictive": "predictive"}.get(d["kind"], d["kind"])
            key = key if key in text.RECT else "replace"
            d["rect"] = text.RECT[key].format(name=spec.name.lower(), pn=spec.pn, old=old.sn, new=new.sn,
                                               insp=insp_name or "")
            self.log["changes"].append(dict(t=self.env.now, tail=ac.tail, slot=slot, pos=spec.pos, pn=spec.pn,
                                            removed_sn=old.sn, installed_sn=new.sn, reason=d["kind"],
                                            defect=d["id"], wo=wo["id"]))

    def planned_removal(self, ac, slot, kind="predictive", why=""):
        """A removal the policy decided on (no troubleshooting: the part is known). Returns the defect."""
        if slot in ac.open_slot or ac.slots.get(slot) is None or ac.flying:
            return None
        spec = ac.slot_spec[slot]
        txt = f"{spec.name.lower()} replacement planned on condition" + (f" ({why})" if why else "")
        d = self._defect(ac, kind, "planning", slot=slot, txt=txt)
        ac.open_slot[slot] = d
        self.env.process(self._planned(ac, d))
        return d

    def _planned(self, ac, d):
        item = d["id"]
        ac.items[item] = "wait"
        self.set_state(ac)
        spec = ac.slot_spec[d["slot"]]
        ag = self.o_agency[ac.unit] if spec.level == "O" else self.i_agency[ac.base]
        wo = self._open_wo(ag, ac, "unscheduled", [item])
        wo["t_started"] = self.env.now
        d["wo"] = wo["id"]
        yield from self._replace(ac, [(d["slot"], d)], item, ag, wo, priority=1)
        d["work_h"] = spec.mttr_h * (1 - self.dl["troubleshoot_frac"])
        ac.open_slot.pop(d["slot"], None)
        yield self.env.timeout(_u(ac.rng, self.dl["admin_h"]))
        d["t_closed"] = wo["t_closed"] = self.env.now
        ac.items.pop(item, None)
        self.set_state(ac)

    def check_job(self, ac, slot, why=""):
        """A targeted inspection of one position after a HUMS alert that the inspection did not
        confirm: the aircraft is down for the diagnosis part of the job, nothing is removed."""
        if slot in ac.open_slot or ac.slots.get(slot) is None or ac.flying:
            return None
        spec = ac.slot_spec[slot]
        d = self._defect(ac, "check", "planning", slot=slot, txt=f"{spec.name.lower()} inspected after HUMS alert" + (f" ({why})" if why else ""))
        ac.open_slot[slot] = d
        self.env.process(self._check(ac, d, spec.mttr_h * self.dl["troubleshoot_frac"]))
        return d

    def _check(self, ac, d, work):
        item = d["id"]
        ac.items[item] = "wait"
        self.set_state(ac)
        spec = ac.slot_spec[d["slot"]]
        ag = self.o_agency[ac.unit] if spec.level == "O" else self.i_agency[ac.base]
        wo = self._open_wo(ag, ac, "unscheduled", [item])
        d["wo"] = wo["id"]
        with self.agency[ag].request(priority=1) as req:
            yield req
            wo["t_started"] = self.env.now
            ac.items[item] = "maint"
            self.set_state(ac)
            yield self.env.timeout(self.cal(ag, work, ac.rng))
        wo["labour_h"] += work
        d["work_h"], d["rect"] = work, "inspected, no fault found; continue to monitor"
        ac.open_slot.pop(d["slot"], None)
        d["t_closed"] = wo["t_closed"] = self.env.now
        ac.items.pop(item, None)
        self.set_state(ac)

    def start_inspection(self, ac, insp):
        """Induct an aircraft for an inspection before it is due (a planner's decision)."""
        if ac.in_inspection or ac.flying:
            return False
        ac.in_inspection = True
        self.env.process(self._inspection(ac, insp))
        return True

    # ================================================================== inspections
    def life_due(self, ac, s, lookahead_fh):
        lim = s.spec.life
        if not lim:
            return False
        if lim.get("fh") and s.age_fh + lookahead_fh >= lim["fh"]:
            return True
        if lim.get("cycles") and s.age_cyc + lookahead_fh * self.cyc_per_fh[ac.unit] >= lim["cycles"]:
            return True
        if lim.get("days"):
            days = (self.env.now - s.cal_t0) / DAY + lookahead_fh / max(self.fh_per_day[ac.unit], 1e-6)
            return days >= lim["days"]
        return False

    def _inspection(self, ac, insp):
        iid = self.nid("IN")
        depot = insp.level == "D"
        status = "depot" if depot else "insp"
        ac.items[iid] = status
        self.set_state(ac)
        ag = {"O": self.o_agency[ac.unit], "I": self.i_agency[ac.base], "D": self.d_agency}[insp.level]
        wo = self._open_wo(ag, ac, "scheduled", inspection=iid)
        rec = dict(id=iid, tail=ac.tail, unit=ac.unit, kind=insp.id, name=insp.name, level=insp.level, agency=ag,
                   wo=wo["id"], t_opened=self.env.now, t_started=None, t_closed=None, fh=ac.fh, findings=0,
                   early=self.due(ac) is None)
        self.log["inspections"].append(rec)
        ac.insp_now = (insp, self.env.now)
        if depot:
            yield self.env.timeout(self.dl["ferry_days"] * DAY)
        with self.agency[ag].request(priority=2) as req:
            yield req
            rec["t_started"] = wo["t_started"] = self.env.now
            yield self.env.timeout(self.cal(ag, insp.work_h, ac.rng))
        wo["labour_h"] += insp.work_h
        look = min(i.interval_fh or 1e9 for i in ac.type.inspections)
        jobs = []
        for slot, s in ac.slots.items():
            open_d = ac.open_slot.get(slot)
            if s is None:
                continue
            spec = s.spec
            if open_d is not None:
                if open_d["deferred"] and open_d["t_closed"] is None and not open_d.get("_in_work"):
                    open_d["_in_work"] = True
                    jobs.append((slot, open_d))
                continue
            p = spec.detect if spec.detect is not None else insp.detect_p
            found = s.p_point <= s.clock() < s.ttf and ac.rng.random() < p
            # the policy may let a monitored unit fly on after a finding (P2); its life limit still applies
            reason = self.policy.inspection_finding(self, ac, insp, slot, s) if found else None
            if reason is None and self.life_due(ac, s, look):
                reason = "life"
            elif reason is None and not found:
                reason = self.policy.inspection_extra(self, ac, insp, slot, s)
            if reason:
                txt = (f"{spec.name.lower()} life due" if reason == "life" else
                       f"{spec.name.lower()} due for preventive replacement" if reason == "preventive" else
                       f"{spec.name.lower()} replacement planned on condition ({insp.name})" if reason == "predictive" else
                       f"{insp.name}: " + text.symptom(spec.pos, ac.rng))
                d = self._defect(ac, reason, "inspection", slot=slot, txt=txt, inspection=iid)
                d["wo"] = wo["id"]
                ac.open_slot[slot] = d
                jobs.append((slot, d))
        for i2 in ac.type.inspections:
            if (i2.interval_fh or 0) <= (insp.interval_fh or 0) or i2.id == insp.id:
                ac.insp_fh[i2.id] = ac.fh
                ac.insp_t[i2.id] = self.env.now
        for d in list(ac.deferred_snags):
            d["rect"], d["t_closed"], d["wo"] = text.RECT["snag"] + f" at {insp.name}", self.env.now, wo["id"]
            ac.deferred_snags.remove(d)
        rec["findings"] = len(jobs)
        if jobs:
            loc = "DP" if depot else ac.base
            yield from self._replace(ac, jobs, iid, ag, wo, priority=2, loc=loc, status=status, insp_name=insp.name)
            for slot, d in jobs:
                d["t_closed"] = self.env.now
                d["work_h"] = ac.slot_spec[slot].mttr_h
                ac.open_slot.pop(slot, None)
        if depot:
            yield self.env.timeout(self.dl["ferry_days"] * DAY)
        yield self.env.timeout(_u(ac.rng, self.dl["admin_h"]))
        rec["t_closed"] = wo["t_closed"] = self.env.now
        ac.items.pop(iid, None)
        ac.in_inspection = False
        ac.insp_now = None
        self.set_state(ac)
        self.check_due(ac)

    # ================================================================== run
    def run(self):
        self.env.run(until=self.days * DAY)
        for ac in self.aircraft:                       # close the state log
            self.log["states"].append(dict(tail=ac.tail, unit=ac.unit, t=self.days * DAY, state="END"))
        return self
