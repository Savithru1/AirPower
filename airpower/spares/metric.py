"""METRIC-style spares allocation: which stock buys the most aircraft availability per rupee.

Model (Sherbrooke's METRIC, single echelon with pooled stock, which matches lateral transfers):
* pipeline per part number: units in repair or on order = demand rate x pipeline time, Poisson;
  rotables: shop turnaround + transit both ways + condemnation share x procurement lead time;
  consumables: procurement lead time. Everything is estimated from the stores records;
* expected backorders EBO(s) = E[(X - s)+] for stock s;
* availability of type t: A_t = prod_i (1 - EBO_it / (N_t Z_it))^Z_it (N_t aircraft, Z_it units
  of part i per aircraft, a shared part's backorders split by demand share);
* greedy marginal allocation on sum_t N_t ln A_t per rupee (optimal for this separable concave
  objective), from zero stock upward: the cost vs availability curve.

Availability here is supply availability (not NMCS); maintenance and scheduled down time come on top.

    python -m airpower.spares.metric build [--db URL]          -> spares tables + curve in the store
"""
import argparse
import heapq
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson
from sqlalchemy import insert, text

from airpower.config import OUTPUTS, db_url
from airpower.model import schema as S


def ebo(mean, s):
    """E[(X - s)+], X ~ Poisson(mean)."""
    if mean <= 0:
        return 0.0
    x = np.arange(0, s + 1)
    return float(mean - s + np.sum((s - x) * poisson.pmf(x, mean)))


class Inputs:
    def __init__(self, url):
        self.eng = S.engine(url)
        q = lambda sql: pd.read_sql(text(sql), self.eng)          # noqa: E731
        self.parts = q("select pn, description, category, unit_cost_inr from part").set_index("pn")
        tx = q("select date, location, pn, sn, txn_type, tail from stock_txn")
        tx["date"] = pd.to_datetime(tx["date"])
        self.tx = tx
        rr = q("select pn, sn, received, completed, finding from repair_return")
        for c in ("received", "completed"):
            rr[c] = pd.to_datetime(rr[c])
        self.rr = rr
        cc = q("select pn, at, tail from component_change")
        cc["at"] = pd.to_datetime(cc["at"])
        self.cc = cc
        inst = q("select i.tail, i.pn, i.removed, a.type_id from installation i join aircraft a on a.tail = i.tail")
        self.fitted = inst[inst["removed"].isna()]
        self.aircraft = q("select tail, type_id, base from aircraft")
        snap = q("select date, location, pn, serviceable, dues_out, in_repair, on_order from stock_snapshot")
        snap["date"] = pd.to_datetime(snap["date"])
        self.snap = snap
        self.days = max((cc["at"].max() - cc["at"].min()).days, 1)

    # ------------------------------------------------------------------ pipelines
    def lead_times(self):
        """Procurement lead time per part by Little's law: mean quantity on order (stock snapshots)
        divided by the order rate (scrap + condemn transactions per day; orders are one-for-one)."""
        snap = self.snap.groupby(["date", "pn"])["on_order"].sum().reset_index()
        on_order = snap.groupby("pn")["on_order"].mean()
        orders = self.tx[self.tx["txn_type"].isin(["SCRAP", "CONDEMN"])].groupby("pn").size()
        span = max((self.snap["date"].max() - self.snap["date"].min()).days, 1)
        out = {}
        for pn, n in orders.items():
            if n >= 3 and pn in on_order.index:
                out[pn] = float(on_order[pn] / (n / span))
        return out

    def pipelines(self):
        rr = self.rr.assign(tat=(self.rr["completed"] - self.rr["received"]).dt.days)
        tat = rr.groupby("pn")["tat"].median()
        ret = self.tx[(self.tx["txn_type"] == "RETURN U/S") & (self.tx["location"] != "DP")]
        last_ret = ret.groupby("sn")["date"].max()
        transit = (rr["received"] - rr["sn"].map(last_ret)).dt.days
        transit = float(np.nanmedian(transit[(transit >= 0) & (transit < 30)])) if transit.notna().any() else 3.0
        cond = self.tx[self.tx["txn_type"] == "CONDEMN"].groupby("pn").size()
        cfrac = (cond.reindex(tat.index).fillna(0) / rr.groupby("pn").size()).fillna(0)
        cfrac_fleet = float(cond.sum() / max(len(rr), 1))
        lead = self.lead_times()
        lead_fleet = float(np.median(list(lead.values()))) if lead else 60.0
        rows = {}
        for pn, p in self.parts.iterrows():
            L = lead.get(pn, lead_fleet)
            if p["category"] == "C":
                T = L
            else:
                T = float(tat.get(pn, np.nan))
                T = (T if np.isfinite(T) else float(tat.median())) + 2 * transit + cfrac.get(pn, cfrac_fleet) * L
            rows[pn] = dict(pipeline_days=T, lead_days=L, transit_days=transit,
                            repair_tat_days=float(tat.get(pn, np.nan)) if pn in tat.index else None)
        return pd.DataFrame(rows).T

    # ------------------------------------------------------------------ fleet structure
    def structure(self):
        """N_t aircraft per type, Z_it units per aircraft, demand share by type."""
        N = self.aircraft.groupby("type_id").size()
        Z = self.fitted.groupby(["pn", "type_id"]).size().unstack(fill_value=0) / N
        rate = self.cc.merge(self.aircraft, on="tail").groupby(["pn", "type_id"]).size().unstack(fill_value=0) / self.days
        return N, Z, rate

    def baseline_stock(self):
        """The stock held at the start of the records (first snapshot), pooled over locations."""
        first = self.snap[self.snap["date"] == self.snap["date"].min()]
        return first.groupby("pn")["serviceable"].sum()

    def current_position(self):
        last = self.snap[self.snap["date"] == self.snap["date"].max()]
        g = last.groupby("pn")[["serviceable", "in_repair", "on_order", "dues_out"]].sum()
        return g, last


class Metric:
    def __init__(self, inp):
        self.inp = inp
        self.N, self.Z, self.rate = inp.structure()
        self.pipe = inp.pipelines()
        self.cost = inp.parts["unit_cost_inr"].astype(float) / 1e5               # INR lakh
        self.skus = [pn for pn in self.Z.index if pn in self.pipe.index]
        self.mean = {pn: float(self.rate.loc[pn].sum() * self.pipe.loc[pn, "pipeline_days"]) if pn in self.rate.index else 0.0
                     for pn in self.skus}

    def contribution(self, pn, s):
        """sum_t N_t Z_it ln(1 - EBO_it(s) / (N_t Z_it)) for one part at stock s."""
        e = ebo(self.mean[pn], s)
        tot = 0.0
        rate = self.rate.loc[pn] if pn in self.rate.index else None
        share_all = float(rate.sum()) if rate is not None else 0.0
        for t, n in self.N.items():
            z = float(self.Z.loc[pn].get(t, 0.0))
            if z <= 0:
                continue
            share = float(rate.get(t, 0.0)) / share_all if share_all > 0 else z / float(self.Z.loc[pn].sum())
            frac = min(e * share / (n * z), 0.999999)
            tot += n * z * math.log(1 - frac)
        return tot

    def availability(self, stock):
        out = {}
        for t, n in self.N.items():
            la = 0.0
            for pn in self.skus:
                z = float(self.Z.loc[pn].get(t, 0.0))
                if z <= 0:
                    continue
                rate = self.rate.loc[pn] if pn in self.rate.index else None
                share_all = float(rate.sum()) if rate is not None else 0.0
                share = float(rate.get(t, 0.0)) / share_all if share_all > 0 else 1.0
                frac = min(ebo(self.mean[pn], int(stock.get(pn, 0))) * share / (n * z), 0.999999)
                la += z * math.log(1 - frac)
            out[t] = math.exp(la)
        fleet = sum(self.N[t] * a for t, a in out.items()) / self.N.sum()
        return fleet, out

    def curve(self, max_cost):
        s = {pn: 0 for pn in self.skus}
        cur = {pn: self.contribution(pn, 0) for pn in self.skus}
        heap = []
        for pn in self.skus:
            gain = self.contribution(pn, 1) - cur[pn]
            heapq.heappush(heap, (-gain / max(self.cost[pn], 1e-6), pn))
        cost, pts, steps = 0.0, [], 0
        a0, _ = self.availability(s)
        pts.append(dict(cost_lakh=0.0, availability=a0))
        while heap and cost < max_cost:
            _, pn = heapq.heappop(heap)
            s[pn] += 1
            cost += self.cost[pn]
            cur[pn] = self.contribution(pn, s[pn])
            gain = self.contribution(pn, s[pn] + 1) - cur[pn]
            heapq.heappush(heap, (-gain / max(self.cost[pn], 1e-6), pn))
            steps += 1
            if steps % 5 == 0 or not heap:
                a, _ = self.availability(s)
                pts.append(dict(cost_lakh=cost, availability=a, stock=dict(s)))
        return pts

    def at_budget(self, pts, budget):
        ok = [p for p in pts if p["cost_lakh"] <= budget and "stock" in p]
        return ok[-1] if ok else pts[0]


def build(url=None):
    inp = Inputs(url or db_url())
    M = Metric(inp)
    base = inp.baseline_stock().reindex(M.skus).fillna(0).astype(int)
    base_cost = float(sum(base[pn] * M.cost[pn] for pn in M.skus))
    a_base, by_type_base = M.availability(base.to_dict())
    pts = M.curve(max_cost=2.0 * base_cost)
    eq = M.at_budget(pts, base_cost)
    a_eq, by_type_eq = M.availability(eq["stock"])
    match = next((p for p in pts if p["availability"] >= a_base and "stock" in p), None)
    pos, last = inp.current_position()
    rows = []
    for pn in M.skus:
        held = int(pos.loc[pn, ["serviceable", "in_repair", "on_order"]].sum()) if pn in pos.index else 0
        tgt = int(eq["stock"].get(pn, 0))
        rows.append(dict(pn=pn, description=inp.parts.loc[pn, "description"], category=inp.parts.loc[pn, "category"],
                         cost_lakh=float(M.cost[pn]), demand_per_day=float(M.rate.loc[pn].sum()) if pn in M.rate.index else 0.0,
                         pipeline_days=float(M.pipe.loc[pn, "pipeline_days"]), pipeline_mean=M.mean[pn],
                         baseline_stock=int(base[pn]), metric_stock=tgt, held_now=held,
                         action="buy" if tgt > held else ("hold" if tgt == held else "reduce"), qty=abs(tgt - held)))
    T = pd.DataFrame(rows)
    summary = dict(baseline_cost_lakh=base_cost, baseline_availability=a_base, baseline_by_type=by_type_base,
                   metric_cost_lakh=eq["cost_lakh"], metric_availability=a_eq, metric_by_type=by_type_eq,
                   metric_cost_to_match_baseline_lakh=match["cost_lakh"] if match else None,
                   curve=[dict(cost_lakh=round(p["cost_lakh"], 1), availability=round(p["availability"], 4)) for p in pts],
                   metric_stock=eq["stock"], baseline_stock={k: int(v) for k, v in base.items()})
    md = S.Base.metadata.tables
    md["spares_target"].create(inp.eng, checkfirst=True)
    with inp.eng.begin() as c:
        c.execute(md["spares_target"].delete())
        c.execute(insert(md["spares_target"]), T.astype(object).where(T.notna(), None).to_dict("records"))
        c.execute(text("delete from meta where key = 'metric'"))
        c.execute(insert(md["meta"]), [dict(key="metric", value=json.dumps(
            {k: v for k, v in summary.items() if k not in ("metric_stock",)}, default=float))])
    summary["store"] = str(inp.eng.url)
    (OUTPUTS / "spares").mkdir(parents=True, exist_ok=True)
    stem = Path(inp.eng.url.database or "store").stem
    (OUTPUTS / "spares" / f"metric_{stem}.json").write_text(json.dumps(summary, indent=1, default=float))
    return summary, T


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    s, T = build(a.db)
    print(f"baseline stock: INR {s['baseline_cost_lakh']:.0f} lakh -> supply availability {s['baseline_availability']:.3f} "
          f"{ {k: round(v, 3) for k, v in s['baseline_by_type'].items()} }")
    print(f"METRIC at the same cost: INR {s['metric_cost_lakh']:.0f} lakh -> {s['metric_availability']:.3f} "
          f"{ {k: round(v, 3) for k, v in s['metric_by_type'].items()} }")
    print(f"METRIC cost to match the baseline availability: INR {s['metric_cost_to_match_baseline_lakh']} lakh")
    print(T.sort_values("cost_lakh", ascending=False)[["pn", "description", "cost_lakh", "pipeline_mean", "baseline_stock", "metric_stock", "held_now", "action"]].head(20).round(2).to_string())
    return 0


if __name__ == "__main__":
    from airpower.spares import metric as _m
    sys.exit(_m.main())
