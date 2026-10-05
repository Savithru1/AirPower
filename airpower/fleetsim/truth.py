"""Truth tables and KPIs of a FleetSim run.

The truth is what really happened in the simulation. The platform never reads it; it is used
only to score the integration layer (tools/reconcile.py) and the models and policies.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .engine import AVAILABLE, DAY, DEPOT, INSP, NMCM, NMCS


def local_datetime(sim, t):
    base = datetime.combine(sim.start, datetime.min.time())
    return base + timedelta(hours=float(t))


def state_intervals(sim):
    s = pd.DataFrame(sim.log["states"]).sort_values(["tail", "t"], kind="stable")
    s["t_end"] = s.groupby("tail")["t"].shift(-1)
    s = s[s["state"] != "END"].dropna(subset=["t_end"])
    s = s[s["t_end"] > s["t"]]
    return s.rename(columns={"t": "t_start"})[["tail", "unit", "state", "t_start", "t_end"]].reset_index(drop=True)


def _clean(rows):
    return pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])


def hums_long(sim):
    rows = []
    for h in sim.log["hums"]:
        for slot, ch in h["channels"].items():
            for name, v in ch.items():
                rows.append((h["sortie"], h["tail"], h["t_landing"], slot, h["serials"][slot], name, float(v)))
    return pd.DataFrame(rows, columns=["sortie", "tail", "t", "slot", "sn", "channel", "value"])


def hums_truth(sim):
    """True age, time to failure and P point of every monitored serial at every HUMS record."""
    rows = []
    for h in sim.log["hums"]:
        for slot, (age, ttf, pp, label) in h.get("truth", {}).items():
            rows.append((h["sortie"], h["tail"], h["t_landing"], slot, h["serials"][slot], age, ttf, pp, label))
    return pd.DataFrame(rows, columns=["sortie", "tail", "t", "slot", "sn", "age", "ttf", "p_point", "fault_label"])


def tables(sim, hums=True):
    """hums=False skips the (large) HUMS tables, which the KPIs do not use."""
    L = sim.log
    out = {
        "states": state_intervals(sim),
        "sorties": pd.DataFrame(L["sorties"]),
        "defects": _clean(L["defects"]),
        "changes": pd.DataFrame(L["changes"]),
        "failures": pd.DataFrame(L["failures"]),
        "inspections": pd.DataFrame(L["inspections"]),
        "work_orders": pd.DataFrame(L["work_orders"]).assign(defects=lambda d: d["defects"].map(lambda x: ",".join(x))),
        "demands": pd.DataFrame(L["demands"]),
        "txns": pd.DataFrame(L["txns"]),
        "repairs": pd.DataFrame(L["repairs"]),
        "procurements": pd.DataFrame(L["procurements"]),
        "stock": pd.DataFrame(L["stock"]),
        "daily": pd.DataFrame(L["daily"]),
        "tasking": pd.DataFrame(L["tasking"]),
        "hums_missing": pd.DataFrame(L["hums_missing"]),
        "hums": hums_long(sim) if hums else pd.DataFrame(),
        "hums_truth": hums_truth(sim) if hums else pd.DataFrame(),
        "serials": pd.DataFrame([dict(sid=s.sid, sn=s.sn, pn=s.pn, rogue=s.rogue, renewals=s.renewals) for s in sim.serials]),
        "aircraft": pd.DataFrame([dict(tail=a.tail, msn=a.msn, hums_unit=a.hums_unit, type=a.type.id, unit=a.unit,
                                       base=a.base, fh_end=a.fh, cycles_end=a.cycles) for a in sim.aircraft]),
    }
    return out


def _window(tab, w0, warmup_days):
    """Rows of the measurement window (after the warm-up)."""
    col = {"sorties": "t_takeoff", "defects": "t_raised", "changes": "t", "failures": "t", "work_orders": "t_opened",
           "demands": "t", "repairs": "t_out", "procurements": "t_arrive"}
    out = dict(tab)
    for k, c in col.items():
        if len(tab[k]):
            out[k] = tab[k][tab[k][c] >= w0]
    for k in ("daily", "tasking"):
        out[k] = tab[k][tab[k]["day"] >= warmup_days]
    st = tab["states"].copy()
    st = st[st["t_end"] > w0]
    st["t_start"] = st["t_start"].clip(lower=w0)
    out["states"] = st
    return out


def kpis(sim, tab):
    w0 = sim.warmup * DAY
    T = sim.days * DAY - w0
    tab = _window(tab, w0, sim.warmup)
    st = tab["states"].assign(dur=lambda d: d["t_end"] - d["t_start"])
    n_ac = defaultdict(int)
    for a in sim.aircraft:
        n_ac[a.unit] += 1
    by = {}
    for unit, g in list(st.groupby("unit")) + [("FLEET", st)]:
        n = len(sim.aircraft) if unit == "FLEET" else n_ac[unit]
        tot = n * T
        dur = g.groupby("state")["dur"].sum()
        by[unit] = dict(aircraft=n,
                        availability=float(dur.reindex(list(AVAILABLE)).fillna(0).sum() / tot),
                        nmcs=float(dur.get(NMCS, 0.0) / tot), nmcm=float(dur.get(NMCM, 0.0) / tot),
                        scheduled=float((dur.get(INSP, 0.0) + dur.get(DEPOT, 0.0)) / tot))
    daily, task = tab["daily"], tab["tasking"]
    for unit in list(n_ac) + ["FLEET"]:
        dd = daily if unit == "FLEET" else daily[daily["unit"] == unit]
        tk = task if unit == "FLEET" else task[task["unit"] == unit]
        by[unit]["serviceable_0800"] = float(dd["serviceable"].sum() / max(dd["aircraft"].sum(), 1))
        by[unit]["task_met"] = float(tk["flown"].sum() / max(tk["tasked"].sum(), 1))
    ch, de, dm = tab["changes"], tab["defects"], tab["demands"]
    bo = dm[dm["backordered"]] if len(dm) else dm
    wait_d = ((bo["t_filled"] - bo["t"]) / DAY).dropna() if len(bo) else pd.Series(dtype=float)
    cost = float(tab["repairs"]["cost"].sum() if len(tab["repairs"]) else 0) + \
        float(tab["procurements"]["cost"].sum() if len(tab["procurements"]) else 0)
    wo = tab["work_orders"]
    return dict(
        policy=sim.policy.name, seed=sim.seed, days=sim.days - sim.warmup, warmup_days=sim.warmup,
        real_signals=sim.bank.real,
        by_unit=by,
        flying=dict(sorties=int(len(tab["sorties"])), fh=float(tab["sorties"]["fh"].sum()),
                    aborted=int(tab["sorties"]["aborted"].sum())),
        removals={str(k): int(v) for k, v in ch["reason"].value_counts().items()} if len(ch) else {},
        in_service_failures=int(len(tab["failures"])),
        defects=dict(total=int(len(de)), snags=int((de["kind"] == "snag").sum()),
                     repeat_snags=int(de.get("repeat", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()),
                     deferred=int(de["deferred"].sum())),
        spares=dict(demands=int(len(dm)), backordered=int(len(bo)),
                    backorder_wait_days_mean=float(wait_d.mean()) if len(wait_d) else 0.0,
                    material_cost_lakh=cost),
        labour_h=float(wo["labour_h"].sum()) if len(wo) else 0.0,
        hums=dict(records=int(len(sim.log["hums"])), missing=int(len(sim.log["hums_missing"]))),
    )


def write(sim, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    tab = tables(sim)
    for name, df in tab.items():
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
    k = kpis(sim, tab)
    (out_dir / "summary.json").write_text(json.dumps(k, indent=1))
    return tab, k
