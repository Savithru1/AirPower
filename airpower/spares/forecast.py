"""Spares demand: prediction-driven forecast vs history-only baselines (plan section 6.1).

Prediction-driven: for every unit fitted at the forecast time t, the probability that it is
removed within the horizon (H days, i.e. its aircraft's recent usage x H flight hours):
* condition, where a HUMS risk record exists (engine / battery remaining-life interval as a
  log-normal; gearbox P(removal within 100 FH) scaled to the horizon);
* age, otherwise: the part's Weibull law conditioned on the unit's age when the records give it,
  else its mean time between confirmed failures;
* no-fault-found removals at the part's NFF rate per component hour;
* life limits from the life cards (hours or calendar), certain when reached within the horizon.
These combine as independent hazards; summing over units gives the expected demand per part
number (and a variance, sum p(1-p)).

Baselines see only past demand: SBA (Syntetos-Boylan approximation of Croston's method, the
usual method for intermittent spares) and the mean daily rate of the last 180 days.

Reliability parameters come from a reliability table (by default the store's own); the
backtest uses a separate training fleet's table, so the forecast is never fitted on the period it
predicts.

    python -m airpower.spares.forecast backtest --db URL --rel-db TRAINING_URL [--horizon 60]
"""
import argparse
import json
import math
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm
from sqlalchemy import text

from airpower.config import OUTPUTS, db_url
from airpower.model import schema as S
from airpower.reliability.lifedata import Fleet

SBA_ALPHA = 0.1


def parse_limit(s):
    out = {}
    if isinstance(s, str):
        for kv in s.split(";"):
            if "=" in kv:
                k, v = kv.split("=")
                out[k.strip()] = float(v)
    return out


class Context:
    """Everything the forecast needs, loaded once from the store."""

    def __init__(self, url=None, rel_url=None):
        self.F = Fleet(url or db_url())
        eng = self.F.eng
        q = lambda sql, e=eng: pd.read_sql(text(sql), e)          # noqa: E731
        self.rel = q("select * from reliability_part", S.engine(rel_url) if rel_url else eng).set_index("pn")
        r = q("select tail, position, sn, kind, as_of, rul_fh, rul_fh_lo, rul_fh_hi, p_fail_100fh from risk_record")
        r["as_of"] = pd.to_datetime(r["as_of"])
        self.risk = r.sort_values("as_of")
        tx = q("select date, location, pn, sn, txn_type, demand_no, tail from stock_txn")
        tx["date"] = pd.to_datetime(tx["date"])
        self.tx = tx
        self.demands = tx[tx["txn_type"] == "DEMAND"]

    def usage(self, t, days=60):
        """Recent flight hours per day of every aircraft."""
        out = {}
        for tail in self.F.cum:
            fh, _ = self.F.usage(tail, [t - pd.Timedelta(days=days)], [t])
            out[tail] = float(fh[0]) / days
        return out

    def fitted(self, t):
        I = self.F.inst
        u = I[(I["installed"].isna() | (I["installed"] <= t)) & (I["removed"].isna() | (I["removed"] > t))].copy()
        ages = []
        for r in u.itertuples():
            if pd.notna(r.installed):
                a = float(self.F.usage(r.tail, [r.installed], [t])[0][0])
            elif pd.isna(r.removed) and pd.notna(r.tso_h_at_export):
                a = float(r.tso_h_at_export) - float(self.F.usage(r.tail, [t], [self.F.t1])[0][0])
            else:
                a = np.nan
            ages.append(a)
        u["age_fh"] = ages
        u["days_fitted"] = (t - u["installed"]).dt.days
        return u


def unit_probabilities(ctx, t, horizon_days):
    """P(removal within the horizon) for every unit fitted at t, with the reason."""
    from airpower.health.fleet_risk import pos_key
    u = ctx.fitted(t)
    use = ctx.usage(t)
    risk = ctx.risk[(ctx.risk["as_of"] <= t) & (ctx.risk["as_of"] > t - pd.Timedelta(days=30))]
    last = risk.groupby(["tail", "sn"]).tail(1).set_index(["tail", "sn"])
    rows = []
    for r in u.itertuples():
        D = use.get(r.tail, 0.0) * horizon_days
        rel = ctx.rel.loc[r.pn] if r.pn in ctx.rel.index else None
        basis = "none"
        p_cond = 0.0
        if (r.tail, r.sn) in last.index:
            k = last.loc[(r.tail, r.sn)]
            if k["kind"] == "gearbox_anomaly":
                p_cond = 1 - (1 - float(k["p_fail_100fh"])) ** (D / 100.0)
            elif pd.notna(k["rul_fh"]):
                med = max(float(k["rul_fh"]), 1.0)
                sig = max(math.log(max(float(k["rul_fh_hi"]), med + 1) / med) / 1.645, 0.05)
                p_cond = float(norm.cdf((math.log(max(D, 1e-6)) - math.log(med)) / sig))
            basis = k["kind"]
        elif rel is not None:
            b, eta = rel.get("weibull_beta"), rel.get("weibull_eta_h")
            if pd.notna(b) and pd.notna(eta) and np.isfinite(r.age_fh):
                a = max(r.age_fh, 0.0)
                p_cond = 1 - math.exp(-(((a + D) / eta) ** b - (a / eta) ** b))
                basis = "weibull_age"
            elif pd.notna(rel.get("mtbf_h")) and rel["mtbf_h"] > 0:
                p_cond = 1 - math.exp(-D / rel["mtbf_h"])
                basis = "mtbf"
        p_nff = 0.0
        if rel is not None and rel.get("component_hours", 0) > 0:
            p_nff = 1 - math.exp(-D * rel["nff"] / rel["component_hours"])
        p_life = 0.0
        lim = parse_limit(r.life_limit)
        if "fh" in lim:
            p_life = 1.0 if (np.isfinite(r.age_fh) and r.age_fh + D >= lim["fh"]) else (
                min(1.0, D / lim["fh"]) if not np.isfinite(r.age_fh) else 0.0)
        if "days" in lim:
            pd_ = 1.0 if (pd.notna(r.days_fitted) and r.days_fitted + horizon_days >= lim["days"]) else (
                min(1.0, horizon_days / lim["days"]) if pd.isna(r.days_fitted) else 0.0)
            p_life = max(p_life, pd_)
        p = 1 - (1 - p_cond) * (1 - p_nff) * (1 - p_life)
        if p_life >= 1.0:
            basis = "life limit"
        rows.append(dict(tail=r.tail, position=pos_key(r.position), pn=r.pn, sn=r.sn, age_fh=r.age_fh, horizon_fh=D,
                         p_condition=p_cond, p_nff=p_nff, p_life=p_life, p=p, basis=basis))
    return pd.DataFrame(rows)


def predict(ctx, t, horizon_days):
    U = unit_probabilities(ctx, t, horizon_days)
    g = U.groupby("pn")
    return pd.DataFrame(dict(mean=g["p"].sum(), var=g["p"].apply(lambda p: float(np.sum(p * (1 - p)))))), U


def sba(ctx, t, horizon_days):
    """Syntetos-Boylan approximation on weekly demand up to t."""
    d = ctx.demands[ctx.demands["date"] <= t]
    weeks = pd.date_range(ctx.F.t0.normalize(), t, freq="7D")
    out = {}
    for pn, g in d.groupby("pn"):
        y = np.histogram(g["date"].astype("int64"), bins=weeks.astype("int64"))[0] if len(weeks) > 1 else np.array([len(g)])
        z = p = None
        q = 1
        for v in y:
            if v > 0:
                z = v if z is None else z + SBA_ALPHA * (v - z)
                p = q if p is None else p + SBA_ALPHA * (q - p)
                q = 1
            else:
                q += 1
        out[pn] = 0.0 if z is None else (1 - SBA_ALPHA / 2) * z / p * horizon_days / 7.0
    return pd.Series(out, dtype=float)


def recent_rate(ctx, t, horizon_days, days=180):
    d = ctx.demands[(ctx.demands["date"] <= t) & (ctx.demands["date"] > t - pd.Timedelta(days=days))]
    return d.groupby("pn").size() / days * horizon_days


def backtest(ctx, horizon_days=60, step_days=28, warm_days=180):
    origins = pd.date_range(ctx.F.t0 + pd.Timedelta(days=warm_days), ctx.F.t1 - pd.Timedelta(days=horizon_days),
                            freq=f"{step_days}D")
    rows = []
    for t in origins:
        pred, _ = predict(ctx, t, horizon_days)
        s, rr = sba(ctx, t, horizon_days), recent_rate(ctx, t, horizon_days)
        act = ctx.demands[(ctx.demands["date"] > t) & (ctx.demands["date"] <= t + pd.Timedelta(days=horizon_days))].groupby("pn").size()
        for pn in sorted(set(pred.index) | set(act.index)):
            m = float(pred["mean"].get(pn, 0.0))
            v = float(pred["var"].get(pn, 0.0))
            rows.append(dict(origin=t, pn=pn, actual=int(act.get(pn, 0)), predictive=m, predictive_sd=math.sqrt(v),
                             sba=float(s.get(pn, 0.0)), recent=float(rr.get(pn, 0.0))))
    B = pd.DataFrame(rows)
    parts = ctx.F.parts.set_index("pn")
    B["category"] = B["pn"].map(parts["category"])
    B["cost"] = B["pn"].map(parts["unit_cost_inr"])
    res = dict(horizon_days=horizon_days, origins=int(len(origins)), part_numbers=int(B["pn"].nunique()),
               total_actual=int(B["actual"].sum()))
    for name, m in (("all", slice(None)), ("rotables", B["category"] == "R"), ("high_value_ge_10_lakh", B["cost"] >= 1e6)):
        b = B[m] if not isinstance(m, slice) else B
        res[name] = {meth: dict(wape=float((b[meth] - b["actual"]).abs().sum() / max(b["actual"].sum(), 1)),
                                bias=float((b[meth] - b["actual"]).sum() / max(b["actual"].sum(), 1)))
                     for meth in ("predictive", "sba", "recent")}
    lo = np.maximum(B["predictive"] - 1.645 * B["predictive_sd"], 0)
    hi = B["predictive"] + 1.645 * B["predictive_sd"]
    res["predictive_interval90_coverage"] = float(((B["actual"] >= np.floor(lo)) & (B["actual"] <= np.ceil(hi))).mean())
    return res, B


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["backtest"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--rel-db", default=None, help="store whose reliability table supplies the part parameters")
    ap.add_argument("--horizon", type=int, default=60)
    a = ap.parse_args(argv)
    ctx = Context(a.db, a.rel_db)
    res, B = backtest(ctx, a.horizon)
    (OUTPUTS / "spares").mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "spares" / "forecast_backtest.json").write_text(json.dumps(res, indent=1, default=str))
    B.to_csv(OUTPUTS / "spares" / "forecast_backtest.csv", index=False)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    from airpower.spares import forecast as _m
    sys.exit(_m.main())
