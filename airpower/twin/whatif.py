"""Fleet twin: FleetSim started from the integrated records and run forward under what-if levers.

The state at the end of the records (from the store, never from the simulator's truth):
* airframe hours and every inspection's last done (inspection_status);
* every fitted component's age (life card hours, installation date for calendar lives);
  remaining lives are drawn conditional on those ages, so the twin is a forecast, not a replay;
* serviceable stock per location, units in repair and on order (latest stock snapshot);
* aircraft already down: in an inspection (for its remaining expected time) or waiting for a part.

Levers: flying rate, an exercise (surge), repair / procurement times, maintenance capacity per
level, stock (as held, or the METRIC recommendation), policy (P0 as today, P2 predictive).
Every forecast is paired with the baseline (no levers) on the same seeds, so the difference
has a confidence interval even when both bands are wide.

    python -m airpower.twin.whatif forecast [--db URL] [--days 30] [--reps 20]
    python -m airpower.twin.whatif run --levers '{"flying": 1.3}' [--days 90]
"""
import argparse
import copy
import hashlib
import json
import math
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

from airpower.config import OUTPUTS, db_url

OUT = OUTPUTS / "twin"
DEFAULTS = dict(flying=1.0, surge=None, lead=1.0, capacity={"O": 0, "I": 0, "D": 0}, stock="current", policy="P0")
SEED0 = 1000
_POOL = None


# ====================================================================== state from the store
def _slot(position, multi):
    m = re.match(r"^(.*)-(\d+)$", str(position))
    return f"{m.group(1)}#{m.group(2)}" if m and m.group(1) in multi else str(position)


def from_store(url=None, utc_offset_h=5.5):
    from airpower.fleetsim.config import load as load_cfg
    from airpower.ingest.resolve import key
    from airpower.model import schema as S
    eng = S.engine(url or db_url())
    q = lambda sql: pd.read_sql(text(sql), eng)                  # noqa: E731
    cfg = load_cfg()
    st = q('select tail, status, reason, start, "end" from aircraft_status')
    for c in ("start", "end"):
        st[c] = pd.to_datetime(st[c])
    t_end = st["end"].max()
    start = (t_end + pd.Timedelta(hours=utc_offset_h)).normalize()          # local day after the records
    ac = q("select tail, type_id, unit_id from aircraft").set_index("tail")
    names = {t.id: {i.name: i for i in t.inspections} for t in cfg.types.values()}
    multi = {t.id: {p.pos for p in t.positions if p.qty > 1} for t in cfg.types.values()}
    ins = q("select tail, name, last_done, last_done_fh, airframe_fh_at_export from inspection_status")
    ins["last_done"] = pd.to_datetime(ins["last_done"])
    aircraft = {}
    for tail, g in ins.groupby("tail"):
        typ = ac.loc[tail, "type_id"]
        a = aircraft.setdefault(tail, dict(fh=float(g["airframe_fh_at_export"].iloc[0]), inspections={}, slots={}))
        for r in g.itertuples():
            i = names[typ].get(r.name)
            if i is None or pd.isna(r.last_done):
                continue
            a["inspections"][i.id] = (float(a["fh"] - r.last_done_fh), float((start - r.last_done).total_seconds() / 86400))
    inst = q("select tail, position, pn, installed, tso_h_at_export from installation where removed is null")
    inst["installed"] = pd.to_datetime(inst["installed"])
    for r in inst.itertuples():
        if r.tail not in aircraft or pd.isna(r.tso_h_at_export):
            continue
        slot = _slot(r.position, multi[ac.loc[r.tail, "type_id"]])
        aircraft[r.tail]["slots"][slot] = dict(age_fh=float(r.tso_h_at_export),
                                               cal_days=None if pd.isna(r.installed) else float((start - r.installed).days))
    # store P/N (after supersession) -> simulator P/N
    alias = q("select alias, pn from part_alias").set_index("alias")["pn"].to_dict()
    to_sim = {}
    for pn in cfg.parts():
        to_sim[alias.get(key(pn), pn)] = pn
        to_sim[pn] = pn
    snap = q("select date, location, pn, serviceable, in_repair, on_order from stock_snapshot "
             "where date = (select max(date) from stock_snapshot)")
    stock, pipe = {}, {}
    for r in snap.itertuples():
        pn = to_sim.get(r.pn)
        if pn is None:
            continue
        stock.setdefault(pn, {})[r.location] = stock.get(pn, {}).get(r.location, 0) + int(r.serviceable)
        p = pipe.setdefault(pn, {"in_repair": 0, "on_order": 0})
        p["in_repair"] += int(r.in_repair or 0)
        p["on_order"] += int(r.on_order or 0)
    dur = q("select name, started, completed from inspection where completed is not null")
    for c in ("started", "completed"):
        dur[c] = pd.to_datetime(dur[c])
    med = dur.assign(d=(dur["completed"] - dur["started"]).dt.total_seconds() / 86400).groupby("name")["d"].median().to_dict()
    open_ = []
    for r in st[st["end"] == t_end].itertuples():
        typ = ac.loc[r.tail, "type_id"] if r.tail in ac.index else None
        if r.status == "SCHEDULED" and typ and r.reason in names[typ]:
            elapsed = (t_end - r.start).total_seconds() / 86400
            open_.append(dict(tail=r.tail, kind="inspection", inspection=names[typ][r.reason].id,
                              remaining_days=max(1.0, med.get(r.reason, 5.0) - elapsed)))
        elif r.status == "NMCS":
            for part in str(r.reason or "").split("; "):
                pn = to_sim.get(part.split(" ", 1)[1] if " " in part else part)
                if pn:
                    open_.append(dict(tail=r.tail, kind="spares", pn=pn))
    return dict(start_date=str(start.date()), records_end_utc=str(t_end), aircraft=aircraft, stock=stock,
                pipeline=pipe, open=open_)


def metric_stock(url=None):
    """The METRIC recommendation held in the store (spares_target), in simulator P/Ns."""
    from airpower.spares.validate import to_sim_pn
    from airpower.model import schema as S
    t = pd.read_sql(text("select pn, metric_stock from spares_target"), S.engine(url or db_url()))
    return to_sim_pn(t.set_index("pn")["metric_stock"].to_dict(), url or db_url())


def validate_levers(levers):
    """Levers within the ranges the twin is meant for; unknown names and bad values are refused.
    Returns the levers with defaults filled in."""
    if not isinstance(levers, dict):
        raise ValueError("levers must be an object")
    unknown = set(levers) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown levers {sorted(unknown)}")

    def num(name, v, lo, hi):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"{name} must be a number between {lo} and {hi}")
        return float(v)
    out = copy.deepcopy(DEFAULTS)
    if "flying" in levers:
        out["flying"] = num("flying", levers["flying"], 0.5, 2.0)
    if "lead" in levers:
        out["lead"] = num("lead", levers["lead"], 0.5, 3.0)
    if levers.get("surge") is not None:
        s = levers["surge"]
        if not isinstance(s, dict) or set(s) - {"start_day", "days", "factor"}:
            raise ValueError("surge must be {start_day, days, factor}")
        out["surge"] = dict(start_day=int(num("surge start_day", s.get("start_day", 0), 0, 170)),
                            days=int(num("surge days", s.get("days", 14), 1, 60)),
                            factor=num("surge factor", s.get("factor", 1.4), 1.0, 2.5))
    if "capacity" in levers:
        c = levers["capacity"]
        if not isinstance(c, dict) or set(c) - {"O", "I", "D"}:
            raise ValueError("capacity must be {O, I, D}")
        out["capacity"] = {k: int(num(f"capacity {k}", c.get(k, 0), -3, 4)) for k in ("O", "I", "D")}
    if "stock" in levers:
        if levers["stock"] not in ("current", "metric"):
            raise ValueError("stock must be current or metric")
        out["stock"] = levers["stock"]
    if "policy" in levers:
        if levers["policy"] not in ("P0", "P2"):
            raise ValueError("policy must be P0 or P2")
        out["policy"] = levers["policy"]
    return out


# ====================================================================== levers -> overrides
def overrides(state, levers, metric=None):
    from airpower.fleetsim.config import load as load_cfg
    lv = dict(copy.deepcopy(DEFAULTS), **(levers or {}))
    raw = load_cfg().raw
    units = copy.deepcopy(raw["units"])
    for u in units:
        u["flying"]["sorties_per_day"] = u["flying"]["sorties_per_day"] * float(lv["flying"])
    ags = copy.deepcopy(raw["agencies"])
    for a in ags:
        if a["level"] in ("O", "I", "D"):
            a["capacity"] = max(1, int(a["capacity"] + int((lv["capacity"] or {}).get(a["level"], 0))))
    s = lv.get("surge")
    surges = [dict(start_day=int(s["start_day"]), days=int(s["days"]), factor=float(s["factor"]))] if s else []
    stores = dict(lead_factor=float(lv["lead"]), pipeline=state["pipeline"])
    if lv["stock"] == "metric" and metric:
        stores["stock_override"] = metric
    else:
        stores["stock_by_loc"] = state["stock"]
    return dict(start_date=state["start_date"], units=units, agencies=ags, surges=surges, stores=stores, log_hums=False), lv


# ====================================================================== runs
def _run(job):
    seed, state, ov, policy, days = job
    from airpower.fleetsim import simulate, truth
    args = dict(planner=None) if policy == "P2" else None
    sim = simulate(seed=seed, policy=policy, policy_args=args, days=days, overrides=ov, state=state)
    tab = truth.tables(sim, hums=False)
    k = truth.kpis(sim, tab)
    d = tab["daily"]
    per_day = d.groupby("day").agg(serviceable=("serviceable", "sum"), aircraft=("aircraft", "sum"))
    by_unit = d.pivot_table(index="day", columns="unit", values="serviceable", aggfunc="sum")
    f = k["by_unit"]["FLEET"]
    return dict(seed=seed, fleet=(per_day["serviceable"] / per_day["aircraft"]).tolist(),
                units={u: by_unit[u].tolist() for u in by_unit.columns},
                availability=f["availability"], task_met=f["task_met"], nmcs=f["nmcs"], nmcm=f["nmcm"],
                scheduled=f["scheduled"], failures=k["in_service_failures"], backorders=k["spares"]["backordered"])


def _pool(procs):
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(procs)
    return _POOL


def _summ(runs, n_ac, start, unit_sizes):
    F = np.array([r["fleet"] for r in runs])
    days = pd.date_range(start, periods=F.shape[1], freq="D")
    ci = lambda v: dict(mean=float(np.mean(v)), ci95=float(1.96 * np.std(v, ddof=1) / math.sqrt(len(v))) if len(v) > 1 else None)  # noqa: E731
    out = dict(daily=[dict(day=str(d.date()), mean=float(F[:, i].mean()), p10=float(np.quantile(F[:, i], 0.1)),
                           p90=float(np.quantile(F[:, i], 0.9))) for i, d in enumerate(days)],
               by_unit={u: [float(np.mean([r["units"][u][i] for r in runs])) / unit_sizes[u] for i in range(F.shape[1])]
                        for u in runs[0]["units"]})
    for m in ("availability", "task_met", "nmcs", "nmcm", "scheduled", "failures", "backorders"):
        out[m] = ci([r[m] for r in runs])
    out["serviceable_aircraft_mean"] = float(F.mean() * n_ac)
    return out


def forecast(levers=None, url=None, days=90, reps=20, procs=10, state=None, use_cache=True):
    """Scenario vs baseline, paired by seed. Results are cached by store, levers, days and reps."""
    url = url or db_url()
    lv = dict(copy.deepcopy(DEFAULTS), **(levers or {}))
    tag = hashlib.sha1(json.dumps([url, lv, days, reps], sort_keys=True, default=str).encode()).hexdigest()[:16]
    cache = OUT / "whatif_cache" / f"{tag}.json"
    if use_cache and cache.exists():
        return json.loads(cache.read_text())
    t0 = time.time()
    state = state or from_store(url)
    metric = metric_stock(url) if lv["stock"] == "metric" else None
    ov_s, lv = overrides(state, lv, metric)
    ov_b, _ = overrides(state, None)
    seeds = [SEED0 + i for i in range(reps)]
    jobs = [(s, state, ov_b, "P0", days) for s in seeds]
    same = lv == DEFAULTS
    if not same:
        jobs += [(s, state, ov_s, lv["policy"], days) for s in seeds]
    try:
        res = list(_pool(procs).map(_run, jobs))
    except BrokenProcessPool:                          # a worker died (memory): start a fresh pool once
        global _POOL
        _POOL = None
        res = list(_pool(procs).map(_run, jobs))
    base, scen = res[:reps], (res[reps:] if not same else res[:reps])
    from airpower.fleetsim.config import load as load_cfg
    sizes = {u["id"]: u["aircraft"] for u in load_cfg().raw["units"]}
    n_ac = sum(sizes.values())
    out = dict(levers=lv, days=days, reps=reps, start_date=state["start_date"], records_end_utc=state["records_end_utc"],
               aircraft=n_ac, open_items=len(state["open"]), baseline=_summ(base, n_ac, state["start_date"], sizes),
               scenario=_summ(scen, n_ac, state["start_date"], sizes), seconds=round(time.time() - t0, 1))
    d = np.array([s["availability"] - b["availability"] for s, b in zip(scen, base)])
    t = np.array([s["task_met"] - b["task_met"] for s, b in zip(scen, base)])
    sv = np.array([np.mean(s["fleet"]) - np.mean(b["fleet"]) for s, b in zip(scen, base)]) * n_ac
    k = lambda v: dict(mean=float(v.mean()), ci95=float(1.96 * v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else 0.0)  # noqa: E731
    out["difference"] = dict(availability=k(d), task_met=k(t), serviceable_aircraft=k(sv))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def _actual(job):
    seed, days_done, days = job
    from airpower.fleetsim import simulate, truth
    sim = simulate(seed=seed, policy="P0", days=days, warmup_days=days_done)      # the same run, continued
    k = truth.kpis(sim, truth.tables(sim, hums=False))
    f = k["by_unit"]["FLEET"]
    return dict(availability=f["availability"], task_met=f["task_met"], nmcs=f["nmcs"], failures=k["in_service_failures"],
                backorders=k["spares"]["backordered"])


def validate(cases=(("sqlite:///outputs/airpower.sqlite", "outputs/fleetsim/p0_s1_365d"),
                    ("sqlite:///outputs/risk_s2.sqlite", "outputs/fleetsim/risk_s2_3y"),
                    ("sqlite:///outputs/risk_s3.sqlite", "outputs/fleetsim/risk_s3_3y")), days=30, reps=20, procs=10):
    """Twin forecast from each store vs what the simulated fleet actually did next (the same FleetSim
    run continued past the end of its records). The twin sees only the store."""
    from airpower.config import ROOT
    rows = []
    for url, run in cases:
        meta = json.loads((ROOT / run / "meta.json").read_text())
        f = forecast(None, url, days=days, reps=reps, procs=procs, use_cache=False)
        act = _pool(procs).submit(_actual, (meta["seed"], meta["days"], days)).result()
        b = f["baseline"]
        sd = b["availability"]["ci95"] * math.sqrt(reps) / 1.96          # spread of single futures
        inside = abs(act["availability"] - b["availability"]["mean"]) <= 1.96 * sd
        rows.append(dict(store=url, run=run, seed=meta["seed"], records_days=meta["days"], forecast_days=days, reps=reps,
                         forecast=dict(availability=b["availability"]["mean"], sd=sd,
                                       nmcs=b["nmcs"]["mean"], failures=b["failures"]["mean"], backorders=b["backorders"]["mean"],
                                       task_met=b["task_met"]["mean"]),
                         actual=act, inside_95_band=bool(inside)))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "twin_validation.json").write_text(json.dumps(rows, indent=1))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["forecast", "run", "state", "validate"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--procs", type=int, default=10)
    ap.add_argument("--levers", default="{}")
    a = ap.parse_args(argv)
    if a.cmd == "state":
        s = from_store(a.db)
        print(json.dumps(dict(start_date=s["start_date"], aircraft=len(s["aircraft"]),
                              slots=sum(len(x["slots"]) for x in s["aircraft"].values()), open=s["open"]), indent=1))
        return 0
    if a.cmd == "validate":
        for r in validate(days=a.days, reps=a.reps, procs=a.procs):
            f, x = r["forecast"], r["actual"]
            print(f"{r['run']}: forecast {f['availability']:.1%} (sd {f['sd']:.1%}), actual {x['availability']:.1%}; "
                  f"failures {f['failures']:.0f} vs {x['failures']}, backorders {f['backorders']:.0f} vs {x['backorders']}")
        return 0
    lv = json.loads(a.levers) if a.cmd == "run" else None
    r = forecast(lv, a.db, days=a.days, reps=a.reps, procs=a.procs, use_cache=False)
    if a.cmd == "forecast":
        OUT.mkdir(parents=True, exist_ok=True)
        stem = Path(str((a.db or db_url())).split("///")[-1]).stem
        (OUT / f"forecast_{stem}.json").write_text(json.dumps(r))
    b, s, d = r["baseline"], r["scenario"], r["difference"]
    print(f"{r['days']} days from {r['start_date']} ({r['reps']} runs, {r['seconds']} s), {r['open_items']} open items at start")
    print(f"  baseline availability {b['availability']['mean']:.1%}, task met {b['task_met']['mean']:.1%}")
    if lv:
        print(f"  scenario availability {s['availability']['mean']:.1%}, task met {s['task_met']['mean']:.1%}; "
              f"difference {d['availability']['mean']:+.1%} +- {d['availability']['ci95']:.1%}")
    return 0


if __name__ == "__main__":
    from airpower.twin import whatif as _m
    sys.exit(_m.main())
