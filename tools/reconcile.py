"""Score the integrated store against FleetSim's truth (the platform itself never reads truth/).

    python -m tools.reconcile outputs/fleetsim/p0_s1_365d [--db URL]

For every kind of injected inconsistency: how many the integration layer fixed, reported or
missed. Then: entity counts, HUMS-to-sortie links, and the serviceability derived from
records against the true serviceability, aircraft by aircraft and day by day.
Writes <run>/reconciliation.json.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from airpower.config import db_url
from airpower.ingest.resolve import key


def q(eng, sql):
    return pd.read_sql(sql, eng)


def score(run_dir, url=None):
    run = Path(run_dir)
    eng = create_engine(url or db_url())
    inj = [json.loads(x) for x in (run / "truth" / "injections.jsonl").read_text().splitlines()]
    by = defaultdict(list)
    for i in inj:
        by[i["kind"]].append(i)
    dq = q(eng, "select * from dq_issue")
    dqk = defaultdict(set)
    for r in dq.itertuples():
        dqk[r.rule].add(r.record_key)
    out = {}

    def rate(kind, ok, how):
        n = len(by[kind])
        k = sum(1 for i in by[kind] if ok(i))
        out[kind] = dict(injected=n, handled=k, rate=round(k / n, 4) if n else None, check=how)

    de = q(eng, "select defect_no, ata, ata_source, duplicate_of from defect").set_index("defect_no")
    rate("defect_duplicate", lambda i: de.at[i["key"], "duplicate_of"] == i["clean"] if i["key"] in de.index else False,
         "copy merged into the original record")
    rate("defect_missing_ata",
         lambda i: i["key"] in de.index and pd.notna(de.at[i["key"], "ata"]) and int(de.at[i["key"], "ata"]) == int(i["clean"]),
         "ATA inferred and equal to the true ATA")
    rate("defect_late_entry", lambda i: i["key"] in dqk["late_entry"], "reported as a timeliness issue")
    wod = q(eng, "select wo_id, defect_no from work_order_defect")
    links = set(zip(wod["wo_id"], wod["defect_no"]))
    rate("wo_missing_defect_link", lambda i: all((i["key"], d) in links for d in i["clean"].split(";")),
         "every dropped work-order -> defect link recovered")
    cc = q(eng, "select ref, pn, sn_off, sn_on from component_change").set_index("ref")
    rate("serial_typo", lambda i: cc.at[i["key"], "sn_off" if i["field"] == "serial_off" else "sn_on"] == i["clean"],
         "serial corrected to the true serial")
    parts = q(eng, "select pn, stock_no from part")
    alias = q(eng, "select alias, pn from part_alias").set_index("alias")["pn"].to_dict()
    rate("pn_format_variant", lambda i: alias.get(key(i["clean"])) == cc.at[i["key"], "pn"],
         "variant resolved to the canonical part")
    rate("pn_superseded", lambda i: alias.get(key(i["clean"])) == alias.get(key(i["dirty"])) is not None,
         "old and new P/N resolve to one part")
    rate("stores_xref_missing", lambda i: i["key"] in set(parts["stock_no"]) and
         alias.get(key(i["clean"])) == parts.set_index("stock_no").at[i["key"], "pn"], "stock number linked to its P/N")
    tx = q(eng, "select demand_no, tail from stock_txn where txn_type = 'DEMAND'").set_index("demand_no")
    rate("stores_missing_tail", lambda i: i["key"] in tx.index and tx.at[i["key"], "tail"] == "J" + i["clean"] or
         (i["key"] in tx.index and str(tx.at[i["key"], "tail"])[1:] == i["clean"]), "aircraft recovered")
    hr = q(eng, "select hums_unit, flight_start from hums_record")
    hk = pd.Series(1, index=hr["hums_unit"] + "@" + pd.to_datetime(hr["flight_start"]).dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    rate("hums_duplicate", lambda i: hk.index.tolist().count(i["key"]) == 1 if False else (hk.get(i["key"], 0) == 1),
         "one record kept")
    rate("hums_missing_download", lambda i: i["key"] in dqk["missing_hums_download"], "reported")

    # entity counts
    xw = pd.read_parquet(run / "truth" / "crosswalk.parquet")
    truth_def = (xw["entity"] == "defect").sum()
    counts = dict(
        defects=dict(truth=int(truth_def), loaded_unique=int(de["duplicate_of"].isna().sum())),
        sorties=dict(truth=int((xw["entity"] == "sortie").sum()), loaded=int(q(eng, "select count(*) n from sortie")["n"][0])),
        component_changes=dict(truth=int((xw["entity"] == "change").sum()), loaded=int(len(cc))),
        work_orders=dict(truth=int((xw["entity"] == "work_order").sum()),
                         loaded=int(q(eng, "select count(*) n from work_order")["n"][0])))
    # HUMS -> sortie links against the crosswalk
    hx = xw[xw["entity"] == "hums"].set_index("source_id")["truth_id"]
    sx = xw[xw["entity"] == "sortie"].set_index("truth_id")["source_id"]
    hl = q(eng, "select hums_unit, flight_start, sortie_ref from hums_record")
    hl["k"] = hl["hums_unit"] + "@" + pd.to_datetime(hl["flight_start"]).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    hl["true_ref"] = hl["k"].map(hx).map(sx)
    hums_link = dict(records=int(len(hl)), linked=int(hl["sortie_ref"].notna().sum()),
                     correct=int((hl["sortie_ref"] == hl["true_ref"]).sum()))
    # serviceability: derived from records vs truth, aircraft x day at 08:00 local
    meta = json.loads((run / "meta.json").read_text())
    off = float(meta["utc_offset_h"])
    st = pd.read_parquet(run / "truth" / "states.parquet")
    start = pd.Timestamp(meta["start_date"])
    iv = q(eng, 'select tail, start, "end", status from aircraft_status')
    iv["start"] = pd.to_datetime(iv["start"])
    rows = []
    for d in range(meta["days"]):
        t_h = d * 24 + 8.0
        when_utc = start + pd.Timedelta(hours=t_h - off)
        tr = st[(st["t_start"] <= t_h) & (st["t_end"] > t_h)].set_index("tail")["state"]
        for tail, g in iv.groupby("tail"):
            k = g["start"].searchsorted(when_utc, side="right") - 1
            got = g["status"].iloc[k] if k >= 0 else None
            truth_s = tr.get("".join(tail))
            truth_s = {"FLYING": "SERVICEABLE", "DEPOT": "SCHEDULED"}.get(truth_s, truth_s)
            rows.append((d, tail, truth_s, got))
    cmp = pd.DataFrame(rows, columns=["day", "tail", "truth", "derived"])
    agree = float((cmp["truth"] == cmp["derived"]).mean())
    sv_t = (cmp["truth"] == "SERVICEABLE").groupby(cmp["day"]).sum()
    sv_d = (cmp["derived"] == "SERVICEABLE").groupby(cmp["day"]).sum()
    conf = pd.crosstab(cmp["truth"], cmp["derived"]).to_dict()
    status = dict(aircraft_days=int(len(cmp)), state_agreement=round(agree, 4),
                  serviceable_per_day_mae=round(float((sv_t - sv_d).abs().mean()), 3),
                  availability_truth=round(float((cmp["truth"] == "SERVICEABLE").mean()), 4),
                  availability_derived=round(float((cmp["derived"] == "SERVICEABLE").mean()), 4),
                  confusion_truth_x_derived={str(k): {str(kk): int(vv) for kk, vv in v.items()} for k, v in conf.items()})
    res = dict(injections=out, counts=counts, hums_sortie_links=hums_link, serviceability=status)
    (run / "reconciliation.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    r = score(a.run_dir, a.db)
    print("injected inconsistency        injected  handled   rate   check")
    for k, v in r["injections"].items():
        print(f"  {k:28s} {v['injected']:7d} {v['handled']:8d}  {v['rate'] if v['rate'] is not None else '-':>6}   {v['check']}")
    print("counts:", r["counts"])
    print("HUMS -> sortie links:", r["hums_sortie_links"])
    s = r["serviceability"]
    print(f"serviceability at 08:00: state agreement {s['state_agreement']:.1%}, serviceable/day MAE "
          f"{s['serviceable_per_day_mae']}, availability truth {s['availability_truth']:.1%} vs derived "
          f"{s['availability_derived']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
