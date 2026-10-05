"""Integrate a set of source exports and load the unified store.

    python -m airpower.ingest.load outputs/fleetsim/p0_s1_365d [--db sqlite:///outputs/airpower.sqlite]

Reads <run>/sources/ (the four systems), resolves and links them (pipeline.py), derives
aircraft serviceability (airpower/twin/status.py), replaces the store's contents and writes
<run>/integration_report.json. Each load rebuilds the integrated tables from the cumulative source
exports of the four systems (live HUMS downloads arrive through the signed API instead); users,
plan decisions and the audit log are kept and the load is recorded in the audit log. The
integration report is written last, so /api/ready is false until a load has finished.
"""
import argparse
import getpass
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import insert

from airpower import audit
from airpower.config import db_url
from airpower.model import migrate
from airpower.model import schema as S
from airpower.twin import status as ST

from .pipeline import Integrator

ORDER = ["location", "aircraft_type", "unit", "aircraft", "agency", "part", "part_alias", "sortie", "hums_record",
         "health_indicator", "defect", "work_order", "work_order_defect", "component_change", "inspection",
         "installation", "inspection_status", "stock_snapshot", "stock_txn", "repair_return", "aircraft_status"]


def _records(df):
    df = df.astype(object).where(df.notna(), None)
    for c in df.columns:
        if len(df) and isinstance(df[c].iloc[0], (np.integer, np.floating)):
            df[c] = df[c].map(lambda v: None if v is None else v.item() if hasattr(v, "item") else v)
    return df.to_dict("records")


def write(eng, tables, issues, report, run_name):
    S.create_all(eng, reset=True)
    md = S.Base.metadata.tables
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with eng.begin() as c:
        bid = c.execute(insert(md["ingest_batch"]).values(
            run=run_name, source="all", started=now, finished=now,
            files=0, records_in=int(sum(sum(v.values()) for v in report["records_in"].values())),
            records_loaded=int(sum(len(v) for v in tables.values())), issues=int(len(issues)))).inserted_primary_key[0]
        for name in ORDER:
            df = tables.get(name)
            if df is None or not len(df):
                continue
            cols = [c_.name for c_ in md[name].columns]
            df = df[[c_ for c_ in cols if c_ in df.columns]]
            recs = _records(df)
            for i in range(0, len(recs), 20000):
                c.execute(insert(md[name]), recs[i:i + 20000])
        if len(issues):
            c.execute(insert(md["dq_issue"]), _records(issues.assign(batch_id=bid)))
        c.execute(insert(md["meta"]), [dict(key="integration_report", value=json.dumps(report, default=str)),
                                        dict(key="loaded_utc", value=now.isoformat()),
                                        dict(key="run", value=run_name)])


def load(run_dir, url=None):
    run_dir = Path(run_dir)
    t0 = time.time()
    tables, issues, report = Integrator(run_dir / "sources").run()
    iv, w0, w1 = ST.derive(tables)
    iv.insert(0, "id", np.arange(1, len(iv) + 1))
    tables["aircraft_status"] = iv
    report["window_utc"] = [str(w0), str(w1)]
    report["seconds"] = round(time.time() - t0, 1)
    eng = S.engine(url or db_url())
    write(eng, tables, issues, report, run_dir.name)
    migrate.upgrade(eng)                      # operational tables (users, audit, decisions) are kept across loads
    audit.log(eng, f"os:{getpass.getuser()}", "data_load", target=run_dir.name,
              detail=dict(tables=report.get("tables"), seconds=report["seconds"], window=report.get("window_utc")))
    (run_dir / "integration_report.json").write_text(json.dumps(report, indent=1, default=str))
    return tables, issues, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    t0 = time.time()
    tables, issues, report = load(a.run_dir, a.db)
    print(f"loaded {a.run_dir} -> {a.db or db_url()} in {time.time() - t0:.0f} s")
    print(f"  tables: {report['tables']}")
    by = issues.groupby(["rule", "resolution"]).size() if len(issues) else {}
    print(f"  data-quality issues: {len(issues)}")
    for (rule, res), n in (by.items() if len(issues) else []):
        print(f"    {rule:26s} {res:8s} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
