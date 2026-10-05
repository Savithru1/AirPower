"""Maintenance planner (plan section 6.2): CP-SAT over a rolling horizon in daily buckets.

Jobs, all derived from the store at the planning time t0:
* scheduled inspections: due when the airframe hours or calendar since the last one reach the
  programme interval (records' inspection status + recent flying rate + registry programme);
  allowed up to FLEX of the interval early, never late; the next occurrence of the same
  inspection follows the chosen start by one interval, so occurrences chain;
* condition jobs: HUMS risk records on watch / alert and life-limited parts reaching their
  limit in the horizon. A soft deadline (when removal becomes likely) and a release day (when a
  spare can be at the base: stock at the base, else transfer, else the part's pipeline);
* blocked days: aircraft already unserviceable at t0, for the expected remaining time.

Decisions: each job's start day (one-hot), and for each condition job either its own induction
or bundling into an inspection of the same aircraft that starts between its release and deadline.
Constraints: agency capacity per day (bays / crews less their usual unscheduled load).
Objective, in order of weight: flying-task shortfall (aircraft short per unit and flying day),
condition-job lateness weighted by its risk, separate inductions, inspections done early
(interval wasted), aircraft-days down.

Baseline for comparison: the due-date rule (induct at the last allowed day, capacity permitting;
condition jobs separately at their deadline; no bundling), scored with the same accounting.

    python -m airpower.plan.planner build [--db URL] [--horizon 90] [--time-limit 60]
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from ortools.sat.python import cp_model
from sqlalchemy import insert, text

from airpower.config import OUTPUTS, db_url
from airpower.health.fleet_risk import pos_key
from airpower.model import schema as S
from airpower.reliability.lifedata import Fleet
from airpower.spares.forecast import parse_limit

REGISTRY = Path(__file__).resolve().parents[1] / "ingest" / "registry.yaml"
FLEX = 0.25
W_SHORT, W_LATE, W_INDUCT, W_EARLY, W_DOWN = 100, 30, 20, 50, 1


class Problem:
    """Jobs, capacities and requirements at t0."""

    def __init__(self, url=None, t0=None, horizon=90, req_factor=1.0):
        self.F = Fleet(url or db_url())
        eng = self.F.eng
        q = lambda sql: pd.read_sql(text(sql), eng)          # noqa: E731
        self.H = horizon
        self.req_factor = req_factor           # > 1: an exercise / surge flying task
        self.t0 = pd.Timestamp(t0) if t0 is not None else self.F.t1.normalize()      # last day of the records
        self.reg = yaml.safe_load(REGISTRY.read_text())
        self.ac = self.F.aircraft.set_index("tail")
        self.days = pd.date_range(self.t0, periods=horizon, freq="D")
        so = q("select tail, takeoff, landing, fh from sortie")
        so["takeoff"] = pd.to_datetime(so["takeoff"])
        self.so = so
        self._usage()
        self._requirements()
        self._agencies(q)
        self.jobs, self.blocked, self.grounded = [], {}, []
        self._inspections(q)
        self._condition(q)
        self._blocked(q)
        self._decisions(q)

    # ------------------------------------------------------------------ inputs
    def _usage(self, days=60):
        s = self.so[self.so["takeoff"] > self.so["takeoff"].max() - pd.Timedelta(days=days)]
        self.usage = (s.groupby("tail")["fh"].sum() / days).reindex(self.ac.index).fillna(0.1).clip(lower=0.1)

    def _requirements(self):
        so = self.so.assign(day=(self.so["takeoff"] + pd.Timedelta(hours=self.reg["records"]["utc_offset_h"])).dt.normalize())
        so["unit"] = so["tail"].map(self.ac["unit_id"])
        self.fly_weekdays = sorted(set(so["day"].dt.weekday.value_counts().loc[lambda v: v > v.max() * 0.2].index))
        per_day = so.groupby(["unit", "day"]).agg(sorties=("tail", "size"))
        per_tail = so.groupby(["unit", "day", "tail"]).size()
        max_per_ac = per_tail.groupby("unit").quantile(0.95).clip(lower=1)
        self.req = {u: int(math.ceil(g["sorties"].quantile(0.75) / max_per_ac[u])) for u, g in per_day.groupby(level=0)}
        self.n_unit = self.ac.groupby("unit_id").size().to_dict()

    def _agencies(self, q):
        ag = q("select id, level, base, capacity from agency")
        wo = q("select agency_id, wo_type, opened, closed from work_order where closed is not null")
        for c in ("opened", "closed"):
            wo[c] = pd.to_datetime(wo[c])
        recent = wo[(wo["wo_type"] == "UNSCHEDULED") & (wo["opened"] > wo["opened"].max() - pd.Timedelta(days=90))]
        load = (recent.assign(d=(recent["closed"] - recent["opened"]).dt.total_seconds() / 86400)
                .groupby("agency_id")["d"].sum() / 90)
        self.cap = {r.id: max(1, int(r.capacity - round(load.get(r.id, 0.0)))) for r in ag.itertuples()}
        self.o_agency = {u: next((a for a in ag["id"] if a.endswith(u)), None) for u in self.n_unit}
        self.i_agency = {r.base: r.id for r in ag.itertuples() if r.level == "I"}
        self.d_agency = next((r.id for r in ag.itertuples() if r.level == "D"), None)

    def agency_for(self, tail, level):
        return {"O": self.o_agency.get(self.ac.loc[tail, "unit_id"]), "I": self.i_agency.get(self.ac.loc[tail, "base"]),
                "D": self.d_agency}.get(level)

    def _inspections(self, q):
        st = q("select tail, name, last_done, last_done_fh, airframe_fh_at_export from inspection_status")
        st["last_done"] = pd.to_datetime(st["last_done"])
        ins = q("select name, started, completed from inspection where completed is not null")
        for c in ("started", "completed"):
            ins[c] = pd.to_datetime(ins[c])
        dur = ins.assign(d=(ins["completed"] - ins["started"]).dt.total_seconds() / 86400).groupby("name")["d"].median()
        self.durations = {k: max(1, int(math.ceil(v))) for k, v in dur.items()}
        prog = self.reg["maintenance_programme"]
        for r in st.itertuples():
            typ = self.ac.loc[r.tail, "type_id"]
            p = next((x for x in prog[typ] if x["name"] == r.name), None)
            if p is None:
                continue
            u = float(self.usage[r.tail])
            af_now = r.airframe_fh_at_export - float(self.F.usage(r.tail, [self.t0], [self.F.t1])[0][0]) \
                if self.t0 < self.F.t1 else r.airframe_fh_at_export + u * (self.t0 - self.F.t1).days
            due = []
            if p.get("interval_fh"):
                due.append((p["interval_fh"] - (af_now - r.last_done_fh)) / u)
            if p.get("interval_days") and pd.notna(r.last_done):
                due.append(p["interval_days"] - (self.t0 - r.last_done).days)
            d0 = int(math.floor(min(due)))
            ivl = min(([p["interval_fh"] / u] if p.get("interval_fh") else []) + ([p["interval_days"]] if p.get("interval_days") else []))
            dur = self.durations.get(r.name, 2)
            k, d = 0, d0
            while d < self.H:
                self.jobs.append(dict(id=f"{r.tail}:{r.name}:{k}", tail=r.tail, kind="inspection", name=r.name, occ=k,
                                      level=p["level"], agency=self.agency_for(r.tail, p["level"]), dur=dur,
                                      due=max(d, 0), interval=max(int(round(ivl)), dur + 1), flex=max(1, int(round(FLEX * ivl))),
                                      overdue=d < 0))
                k += 1
                d = d + int(round(ivl))

    def _condition(self, q):
        risk = q("select tail, position, sn, pn, kind, as_of, rul_fh, p_fail_100fh, status from risk_record")
        risk["as_of"] = pd.to_datetime(risk["as_of"])
        risk = risk[(risk["as_of"] <= self.t0) & (risk["as_of"] > self.t0 - pd.Timedelta(days=30))]
        last = risk.sort_values("as_of").groupby(["tail", "position", "kind"]).tail(1)
        I = self.F.inst
        fitted = I[(I["installed"].isna() | (I["installed"] <= self.t0)) & (I["removed"].isna() | (I["removed"] > self.t0))]
        fitted_sn = set(zip(fitted["tail"], fitted["sn"]))
        snap = q("select date, location, pn, serviceable from stock_snapshot")
        snap["date"] = pd.to_datetime(snap["date"])
        snap = snap[snap["date"] == snap[snap["date"] <= self.t0]["date"].max()]
        stock = snap.groupby(["pn", "location"])["serviceable"].sum()
        pipe = self._pipelines(q)
        jobdur, joblvl = self._repair_jobs(q)
        def release(tail, pn):
            base = self.ac.loc[tail, "base"]
            if stock.get((pn, base), 0) > 0:
                return 0, "spare at base"
            if snap[(snap["pn"] == pn)]["serviceable"].sum() > 0:
                return 3, "transfer from another store"
            return int(math.ceil(pipe.get(pn, 60))), "no spare: pipeline"
        for r in last[last["status"] != "ok"].itertuples():
            if (r.tail, r.sn) not in fitted_sn or r.pn is None:
                continue
            u = float(self.usage[r.tail])
            if pd.notna(r.rul_fh):
                deadline = int(max(0, r.rul_fh / u))
            else:                                  # gearbox: removal due within ~100 FH once flagged
                deadline = int(max(0, 100 / u * (1 - r.p_fail_100fh)))
            rel, why = release(r.tail, r.pn)
            lvl = joblvl.get(r.pn, "O")
            self.jobs.append(dict(id=f"{r.tail}:{r.position}:{r.kind}", tail=r.tail, kind="condition",
                                  name=f"{r.position} {r.kind.replace('_', ' ')}", pn=r.pn, level=lvl,
                                  agency=self.agency_for(r.tail, lvl), dur=jobdur.get(r.pn, 1), deadline=deadline,
                                  release=rel, weight=max(float(r.p_fail_100fh or 0.2), 0.2), reason=f"HUMS {r.status}; {why}"))
        for r in fitted.itertuples():
            lim = parse_limit(r.life_limit)
            if not lim or r.pn is None:
                continue
            u = float(self.usage[r.tail])
            days_left = []
            if "fh" in lim:
                if pd.notna(r.installed):
                    age = float(self.F.usage(r.tail, [r.installed], [self.t0])[0][0])
                elif pd.notna(r.tso_h_at_export):
                    age = float(r.tso_h_at_export) - float(self.F.usage(r.tail, [self.t0], [self.F.t1])[0][0])
                else:
                    age = None
                if age is not None:
                    days_left.append((lim["fh"] - age) / u)
            if "days" in lim and pd.notna(r.installed):
                days_left.append(lim["days"] - (self.t0 - r.installed).days)
            if days_left and min(days_left) < self.H:
                rel, why = release(r.tail, r.pn)
                lvl = joblvl.get(r.pn, "O")
                dl = int(max(0, min(days_left)))
                if rel > dl:      # it cannot fly past its life limit: grounded until the spare can be fitted
                    self.grounded.append((r.tail, dl, min(self.H, rel), f"{pos_key(r.position)} at life limit, awaiting spare"))
                self.jobs.append(dict(id=f"{r.tail}:{pos_key(r.position)}:life", tail=r.tail, kind="condition",
                                      name=f"{pos_key(r.position)} life limit", pn=r.pn, level=lvl,
                                      agency=self.agency_for(r.tail, lvl), dur=jobdur.get(r.pn, 1),
                                      deadline=int(max(0, min(days_left))), release=rel, weight=1.0, hard=True,
                                      reason=f"life limit; {why}"))

    def _pipelines(self, q):
        try:
            from airpower.spares.metric import Inputs
            return Inputs(str(self.F.eng.url)).pipelines()["pipeline_days"].astype(float).to_dict()
        except Exception:
            return {}

    def _repair_jobs(self, q):
        cc = q("select c.pn, d.wo_id from component_change c join defect d on d.defect_no = c.defect_no")
        wo = q("select wo_id, agency_id, opened, closed from work_order where closed is not null")
        for c in ("opened", "closed"):
            wo[c] = pd.to_datetime(wo[c])
        ag = q("select id, level from agency").set_index("id")["level"]
        m = cc.merge(wo, on="wo_id")
        m["d"] = (m["closed"] - m["opened"]).dt.total_seconds() / 86400
        m["level"] = m["agency_id"].map(ag)
        dur = m.groupby("pn")["d"].median().apply(lambda v: max(1, int(math.ceil(min(v, 10)))))
        lvl = m.groupby("pn")["level"].agg(lambda v: v.value_counts().index[0])
        lvl = lvl.where(lvl.isin(["O", "I"]), "O")
        return dur.to_dict(), lvl.to_dict()

    def _blocked(self, q):
        st = q('select tail, start, "end", status, reason from aircraft_status')
        for c in ("start", "end"):
            st[c] = pd.to_datetime(st[c])
        cur = st[(st["start"] <= self.t0) & (st["end"] > self.t0)]
        tx = q("select demand_no, date, txn_type from stock_txn where demand_no is not null")
        tx["date"] = pd.to_datetime(tx["date"])
        dm = tx[tx["txn_type"] == "DEMAND"].groupby("demand_no")["date"].min()
        iss = tx[tx["txn_type"] == "ISSUE"].groupby("demand_no")["date"].min()
        waits = (iss.reindex(dm.index) - dm).dt.days.dropna()
        nmcs_wait = float(waits[waits > 0].median()) if (waits > 0).any() else 14.0
        for r in cur.itertuples():
            if r.status == "SERVICEABLE":
                continue
            elapsed = (self.t0 - r.start).days
            if r.status == "NMCS":
                n = max(1, int(round(nmcs_wait - elapsed)))
            elif r.status == "SCHEDULED":
                n = max(1, int(round(np.median(list(self.durations.values())) - elapsed)))
            else:
                n = 1
            self.blocked[r.tail] = (min(n, self.H), r.status)

    def _decisions(self, q):
        """Jobs an engineering officer accepted or moved (plan_decision) keep that date: the latest
        decision per aircraft and job fixes the first matching job's start day, if inside the horizon."""
        try:
            dec = q("select tail, job, decision, start_date from plan_decision where start_date is not null order by id")
        except Exception:
            return
        dec["start_date"] = pd.to_datetime(dec["start_date"])
        self.fixed = []
        for r in dec.groupby(["tail", "job"]).tail(1).itertuples():
            d = int((r.start_date.normalize() - self.t0.normalize()).days)
            j = next((j for j in sorted(self.jobs, key=lambda j: j.get("occ", 0)) if j["tail"] == r.tail and j["name"] == r.job), None)
            if j is not None and 0 <= d < self.H:
                j["fixed"] = d
                self.fixed.append((r.tail, r.job, d, r.decision))

    def required(self, unit, d):
        return int(math.ceil(self.req[unit] * self.req_factor)) if self.days[d].weekday() in self.fly_weekdays else 0


# ====================================================================== accounting
def evaluate(P, starts, bundled):
    """starts: {job id: start day} for jobs done separately; bundled: {condition id: inspection id}."""
    H = P.H
    down = {t: np.zeros(H, bool) for t in P.ac.index}
    for t, (n, _) in P.blocked.items():
        down[t][:n] = True
    for t, a, b, _ in P.grounded:
        down[t][a:b] = True
    jobs = {j["id"]: j for j in P.jobs}
    for jid, s in starts.items():
        j = jobs[jid]
        down[j["tail"]][max(0, s): min(H, s + j["dur"])] = True
    rows, short = [], 0
    for u, n in P.n_unit.items():
        tails = P.ac.index[P.ac["unit_id"] == u]
        for d in range(H):
            avail = int(n - sum(down[t][d] for t in tails))
            req = P.required(u, d)
            sf = max(0, req - avail)
            short += sf
            rows.append(dict(unit=u, day=d, available=avail, required=req, shortfall=sf))
    late = 0
    for j in P.jobs:
        if j["kind"] != "condition" or j["deadline"] >= H:
            continue
        if j["id"] in starts:
            late += max(0, starts[j["id"]] - j["deadline"])
        elif j["id"] not in bundled:
            late += H - j["deadline"]                 # not done inside the horizon (e.g. no spare in time)
    cond = [j for j in P.jobs if j["kind"] == "condition"]
    return dict(shortfall_aircraft_days=int(short), down_aircraft_days=int(sum(v.sum() for v in down.values())),
                inductions=int(len(starts)), condition_jobs=len(cond), bundled=len(bundled),
                condition_late_days=int(late)), pd.DataFrame(rows)


def due_date_plan(P):
    """Baseline: last allowed day, capacity permitting (else earlier, then later); no bundling."""
    use = {a: np.zeros(P.H + 400, int) for a in set(j["agency"] for j in P.jobs)}
    starts = {}
    order = sorted(P.jobs, key=lambda j: (j.get("due", j.get("deadline", 0)), j["occ"] if "occ" in j else 0))
    last_start = {}
    for j in order:
        if j["kind"] == "inspection":
            key = (j["tail"], j["name"])
            target = j["due"] if j["occ"] == 0 else min(last_start[key] + j["interval"], P.H - 1)
            cand = list(range(target, -1, -1)) + list(range(target + 1, P.H + 300))
        else:
            target = max(j["release"], j["deadline"])
            cand = list(range(target, P.H + 300))
        for s in cand:
            cap = P.cap.get(j["agency"], 1)
            if np.all(use[j["agency"]][s:s + j["dur"]] < cap):
                use[j["agency"]][s:s + j["dur"]] += 1
                starts[j["id"]] = s
                if j["kind"] == "inspection":
                    last_start[(j["tail"], j["name"])] = s
                break
    starts = {k: v for k, v in starts.items() if v < P.H}
    return starts, {}


# ====================================================================== CP-SAT
def optimise(P, time_limit=60, workers=8):
    H = P.H
    m = cp_model.CpModel()
    x, s_var, present = {}, {}, {}
    insp = [j for j in P.jobs if j["kind"] == "inspection"]
    cond = [j for j in P.jobs if j["kind"] == "condition"]
    for j in P.jobs:
        lo, hi = 0, H - 1
        if j.get("fixed") is not None:                   # accepted / moved by an engineering officer
            lo = hi = j["fixed"]
        elif j["kind"] == "condition" and j["release"] >= H:
            lo, hi = 1, 0                                # empty domain: absent
        elif j["kind"] == "inspection" and j["occ"] == 0:
            lo, hi = max(0, j["due"] - j["flex"]), min(j["due"], H - 1)
        elif j["kind"] == "condition":
            lo = min(j["release"], H - 1)
            if j.get("hard"):
                hi = max(lo, min(j["deadline"], H - 1))
        x[j["id"]] = {d: m.NewBoolVar("") for d in range(lo, hi + 1)}
        present[j["id"]] = m.NewBoolVar("")
        m.Add(sum(x[j["id"]].values()) == present[j["id"]])
        s_var[j["id"]] = m.NewIntVar(0, H - 1, "")
        m.Add(s_var[j["id"]] == sum(d * v for d, v in x[j["id"]].items())).OnlyEnforceIf(present[j["id"]])
    # inspections: first occurrence mandatory; later occurrences chained to the previous one
    by_key = {}
    for j in insp:
        by_key.setdefault((j["tail"], j["name"]), []).append(j)
    for occ in by_key.values():
        occ.sort(key=lambda j: j["occ"])
        m.Add(present[occ[0]["id"]] == 1)
        for a, b in zip(occ, occ[1:]):
            # b is due one interval after a's chosen start; mandatory if that falls inside the horizon
            due_b = m.NewIntVar(0, 10 * H, "")
            m.Add(due_b == s_var[a["id"]] + b["interval"])
            inside = m.NewBoolVar("")
            m.Add(due_b <= H - 1).OnlyEnforceIf(inside)
            m.Add(due_b >= H).OnlyEnforceIf(inside.Not())
            m.Add(present[b["id"]] == inside)
            if b.get("fixed") is None:
                m.Add(s_var[b["id"]] <= due_b).OnlyEnforceIf(present[b["id"]])
                m.Add(s_var[b["id"]] >= due_b - b["flex"]).OnlyEnforceIf(present[b["id"]])
    # condition jobs: own induction or bundled into an inspection of the same aircraft
    bundle, late, obj = {}, {}, []
    for c in cond:
        opts = [k for k in insp if k["tail"] == c["tail"]]
        bvars = []
        for k in opts:
            b = m.NewBoolVar("")
            bundle[(c["id"], k["id"])] = b
            m.AddImplication(b, present[k["id"]])
            m.Add(s_var[k["id"]] >= c["release"]).OnlyEnforceIf(b)
            m.Add(s_var[k["id"]] <= max(c["deadline"], c["release"])).OnlyEnforceIf(b)
            bvars.append(b)
        if c["release"] >= H:                          # no spare inside the horizon: cannot be done now
            m.Add(present[c["id"]] + sum(bvars) <= 1)
        else:
            m.Add(present[c["id"]] + sum(bvars) == 1)
        lt = m.NewIntVar(0, H, "")
        m.Add(lt >= s_var[c["id"]] - c["deadline"]).OnlyEnforceIf(present[c["id"]])
        late[c["id"]] = lt
        obj += [int(W_LATE * c["weight"] * 10) * lt, W_INDUCT * 10 * present[c["id"]]]
    # coverage, capacity, downtime, shortfall
    cover = {}
    for j in P.jobs:
        for d in range(H):
            terms = [v for s, v in x[j["id"]].items() if s <= d < s + j["dur"]]
            if terms:
                cover[(j["id"], d)] = terms
    cap_day = getattr(P, "cap_day", None)               # per-day capacity when work is already in progress
    soft = getattr(P, "soft_capacity", False)            # over capacity allowed: the work queues, which costs about
                                                         # a day down for each job-day over (twice W_DOWN)
    for a in set(j["agency"] for j in P.jobs):
        for d in range(H):
            terms = [t for j in P.jobs if j["agency"] == a for t in cover.get((j["id"], d), [])]
            if terms:
                cap = cap_day(a, d) if cap_day else P.cap.get(a, 1)
                if soft:
                    over = m.NewIntVar(0, len(terms), "")
                    m.Add(sum(terms) <= cap + over)
                    obj.append(2 * W_DOWN * 10 * over)
                else:
                    m.Add(sum(terms) <= cap)
    down = {}
    for t in P.ac.index:
        nb = P.blocked.get(t, (0, None))[0]
        fixed = {d for tt, a, b, _ in P.grounded if tt == t for d in range(a, b)}
        for d in range(H):
            v = m.NewBoolVar("")
            down[(t, d)] = v
            if d < nb or d in fixed:
                m.Add(v == 1)
            for j in P.jobs:
                if j["tail"] == t:
                    for term in cover.get((j["id"], d), []):
                        m.AddImplication(term, v)
    for u, n in P.n_unit.items():
        tails = list(P.ac.index[P.ac["unit_id"] == u])
        for d in range(H):
            req = P.required(u, d)
            if req <= 0:
                continue
            sf = m.NewIntVar(0, n, "")
            m.Add(sf >= req - (n - sum(down[(t, d)] for t in tails)))
            obj.append(W_SHORT * 10 * sf)
    for j in insp:
        # days early relative to the due day (first occurrence) or to one interval after the previous one
        if j["occ"] == 0:
            early = m.NewIntVar(0, H, "")
            m.Add(early >= j["due"] - s_var[j["id"]]).OnlyEnforceIf(present[j["id"]])
            obj.append(int(round(W_EARLY * 10 / max(j["interval"], 1))) * early)
    obj.append(W_DOWN * 10 * sum(down.values()))
    m.Minimize(sum(obj))
    sol = cp_model.CpSolver()
    sol.parameters.max_time_in_seconds = time_limit
    sol.parameters.num_workers = workers
    t0 = time.time()
    st = sol.Solve(m)
    status = sol.StatusName(st)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, None, dict(status=status, seconds=round(time.time() - t0, 1))
    starts = {j["id"]: int(sol.Value(s_var[j["id"]])) for j in P.jobs if sol.Value(present[j["id"]])}
    bundled = {c: k for (c, k), b in bundle.items() if sol.Value(b)}
    return starts, bundled, dict(status=status, seconds=round(time.time() - t0, 1), objective=sol.ObjectiveValue(),
                                 bound=sol.BestObjectiveBound(),
                                 variables=len(m.Proto().variables), constraints=len(m.Proto().constraints))


def build(url=None, horizon=90, time_limit=60, t0=None, store=True, req_factor=1.0, write_file=True):
    P = Problem(url, t0=t0, horizon=horizon, req_factor=req_factor)
    b_starts, b_bund = due_date_plan(P)
    base, base_daily = evaluate(P, b_starts, b_bund)
    o_starts, o_bund, info = optimise(P, time_limit)
    opt, opt_daily = evaluate(P, o_starts, o_bund) if o_starts is not None else (None, None)
    summary = dict(t0=str(P.t0), horizon_days=horizon, requirement_factor=req_factor, aircraft=int(len(P.ac)), jobs=len(P.jobs),
                   inspections=sum(1 for j in P.jobs if j["kind"] == "inspection"),
                   condition_jobs=sum(1 for j in P.jobs if j["kind"] == "condition"),
                   blocked_aircraft=len(P.blocked), grounded_awaiting_spares=[dict(tail=t, from_day=a, to_day=b, why=w) for t, a, b, w in P.grounded],
                   requirement_per_flying_day=P.req, capacity_for_planned_work=P.cap,
                   fixed_by_decision=[dict(tail=t, job=j, day=d, decision=x) for t, j, d, x in getattr(P, "fixed", [])],
                   solver=info, due_date_plan=base, optimised_plan=opt)
    if store:
        rows = []
        for name, starts, bund in (("optimised", o_starts or {}, o_bund or {}), ("due_date", b_starts, b_bund)):
            for j in P.jobs:
                s = starts.get(j["id"])
                rows.append(dict(plan=name, tail=j["tail"], unit=P.ac.loc[j["tail"], "unit_id"], job=j["name"], kind=j["kind"],
                                 agency=j["agency"], start_day=s, end_day=None if s is None else s + j["dur"],
                                 due_day=j.get("due", j.get("deadline")), bundled_into=bund.get(j["id"]),
                                 reason=j.get("reason")))
            for t, (n, why) in P.blocked.items():
                rows.append(dict(plan=name, tail=t, unit=P.ac.loc[t, "unit_id"], job=f"currently {why}", kind="blocked",
                                 agency=None, start_day=0, end_day=n, due_day=None, bundled_into=None, reason="status at t0"))
            for t, a, b, why in P.grounded:
                rows.append(dict(plan=name, tail=t, unit=P.ac.loc[t, "unit_id"], job=why, kind="blocked", agency=None,
                                 start_day=a, end_day=b, due_day=None, bundled_into=None, reason="life limit without a spare"))
        md = S.Base.metadata.tables
        md["plan_job"].create(P.F.eng, checkfirst=True)
        R = pd.DataFrame(rows)
        R.insert(0, "id", np.arange(1, len(R) + 1))
        daily = dict(optimised=opt_daily.to_dict("records") if opt_daily is not None else [],
                     due_date=base_daily.to_dict("records"))
        with P.F.eng.begin() as c:
            c.execute(md["plan_job"].delete())
            c.execute(insert(md["plan_job"]), R.astype(object).where(R.notna(), None).to_dict("records"))
            c.execute(text("delete from meta where key in ('plan', 'plan_daily')"))
            c.execute(insert(md["meta"]), [dict(key="plan", value=json.dumps(summary, default=str)),
                                           dict(key="plan_daily", value=json.dumps(daily, default=int))])
    (OUTPUTS / "plan").mkdir(parents=True, exist_ok=True)
    if write_file:
        name = "plan_summary.json" if req_factor == 1.0 else f"plan_summary_surge{req_factor:g}.json"
        (OUTPUTS / "plan" / name).write_text(json.dumps(summary, indent=1, default=str))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=int, default=90)
    ap.add_argument("--time-limit", type=int, default=60)
    ap.add_argument("--t0", default=None)
    ap.add_argument("--surge", type=float, default=1.0, help="multiply the daily flying requirement")
    ap.add_argument("--no-store", action="store_true")
    a = ap.parse_args(argv)
    s = build(a.db, a.horizon, a.time_limit, a.t0, store=not a.no_store, req_factor=a.surge)
    print(json.dumps({k: v for k, v in s.items() if k not in ("capacity_for_planned_work",)}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    from airpower.plan import planner as _m
    sys.exit(_m.main())
