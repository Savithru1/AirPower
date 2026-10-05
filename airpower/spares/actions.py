"""AOG risk: fitted units likely to need a replacement before a spare could arrive, at bases
with no serviceable spare. One row per unit, with whether another store holds one (a transfer
would help) or none does (only the pipeline will).

    python -m airpower.spares.actions build [--db URL] [--threshold 0.3]
"""
import argparse
import sys

import numpy as np
import pandas as pd
from sqlalchemy import insert, text

from airpower.config import db_url
from airpower.model import schema as S
from airpower.spares.forecast import Context, unit_probabilities
from airpower.spares.metric import Inputs


def build(url=None, threshold=0.3, t=None):
    url = url or db_url()
    ctx = Context(url)
    t = pd.Timestamp(t) if t is not None else ctx.F.t1.normalize()
    pipe = Inputs(url).pipelines()["pipeline_days"].astype(float)
    snap = pd.read_sql(text("select date, location, pn, serviceable from stock_snapshot"), ctx.F.eng)
    snap["date"] = pd.to_datetime(snap["date"])
    snap = snap[snap["date"] == snap[snap["date"] <= t]["date"].max()]
    base_of = ctx.F.aircraft.set_index("tail")["base"]
    by_h = {}
    for h in (30, 60, 90):
        by_h[h] = unit_probabilities(ctx, t, h).set_index(["tail", "sn"])
    rows = []
    U = by_h[30]
    for (tail, sn), r in U.iterrows():
        lead = float(pipe.get(r["pn"], 60.0))
        h = 30 if lead <= 30 else (60 if lead <= 60 else 90)
        p = float(by_h[h].loc[(tail, sn), "p"])
        if p < threshold:
            continue
        base = base_of[tail]
        base_stock = int(snap[(snap["pn"] == r["pn"]) & (snap["location"] == base)]["serviceable"].sum())
        if base_stock > 0:
            continue
        fleet_stock = int(snap[snap["pn"] == r["pn"]]["serviceable"].sum())
        exp_dem = float(by_h[h][by_h[h]["pn"] == r["pn"]]["p"].sum())
        rows.append(dict(tail=tail, position=r["position"], pn=r["pn"], sn=sn, p_within_lead=p, lead_days=lead,
                         basis=by_h[h].loc[(tail, sn), "basis"], base_stock=base_stock, fleet_stock=fleet_stock,
                         expected_demand_in_lead=exp_dem))
    R = pd.DataFrame(rows).sort_values("p_within_lead", ascending=False) if rows else pd.DataFrame(
        columns=["tail", "position", "pn", "sn", "p_within_lead", "lead_days", "basis", "base_stock", "fleet_stock",
                 "expected_demand_in_lead"])
    R.insert(0, "id", np.arange(1, len(R) + 1))
    md = S.Base.metadata.tables
    md["aog_risk"].create(ctx.F.eng, checkfirst=True)
    with ctx.F.eng.begin() as c:
        c.execute(md["aog_risk"].delete())
        if len(R):
            c.execute(insert(md["aog_risk"]), R.astype(object).where(R.notna(), None).to_dict("records"))
    return R


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--threshold", type=float, default=0.3)
    a = ap.parse_args(argv)
    R = build(a.db, a.threshold)
    print(f"{len(R)} units at AOG risk")
    print(R.head(15).round(2).to_string())
    return 0


if __name__ == "__main__":
    from airpower.spares import actions as _m
    sys.exit(_m.main())
