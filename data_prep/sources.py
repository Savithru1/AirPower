"""Resolve and load the real datasets listed in data/manifest.json.

'local' sources live under data/raw/<id>/; 'aerotwin' sources are read in place from
$AEROTWIN_DATA (AeroTwin's data/ folder), where AeroTwin already fetched and prepared them.

    python -m data_prep.sources            # availability report
"""
import json
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow.dataset as pads

from airpower.config import AEROTWIN_DATA, MANIFEST, RAW


@lru_cache(maxsize=1)
def manifest():
    return json.loads(MANIFEST.read_text())


def source(sid):
    for s in manifest()["sources"]:
        if s["id"] == sid:
            return s
    raise KeyError(f"unknown source {sid!r}")


def paths(sid):
    s = source(sid)
    if s["location"] == "aerotwin":
        return [AEROTWIN_DATA / f["path"] for f in s["files"]]
    return [RAW / sid / f["dest"] for f in s["files"]]


def available(sid):
    s = source(sid)
    if s["status"] in ("to_verify", "to_fetch"):
        return False
    if sid == "cmapss":
        return (RAW / "cmapss" / "train_FD001.txt").exists()
    ps = paths(sid)
    return bool(ps) and all(p.exists() for p in ps)


def _need(sid):
    if not available(sid):
        where = AEROTWIN_DATA if source(sid)["location"] == "aerotwin" else RAW / sid
        raise FileNotFoundError(f"source {sid!r} not found under {where} (see data/manifest.json)")


def load_vibration_windows():
    """CWRU bearing + SEU gearbox windows: Vib_RMS, Vib_Kurtosis, crest, env_spec_kurt, dataset,
    label, fault_family, condition, rpm, source."""
    _need("vibration")
    return pd.read_parquet(paths("vibration")[0])


def load_battery_cycles():
    """NASA battery: one row per cell and discharge cycle (capacity_ah, soh, ...)."""
    _need("nasa_battery")
    return pd.read_parquet(paths("nasa_battery")[0])


def load_ngafid_labels(sid="ngafid_2days"):
    """One row per flight (index Master Index = flight_id): before_after, label, fold, ..."""
    _need(sid)
    return pd.read_parquet(paths(sid)[0])


def read_ngafid_flights(sid="ngafid_2days", flight_ids=None, columns=None):
    """1 Hz harmonised NGAFID telemetry (contract-v2 names, SI units) for the given flights."""
    _need(sid)
    ds = pads.dataset(str(paths(sid)[1]), format="parquet")
    flt = pads.field("flight_id").isin(list(flight_ids)) if flight_ids is not None else None
    cols = None if columns is None else ["flight_id", "time"] + [c for c in columns if c not in ("flight_id", "time")]
    return ds.to_table(filter=flt, columns=cols).to_pandas()


def report():
    rows = []
    for s in manifest()["sources"]:
        rows.append((s["id"], s["location"], s["status"], "yes" if available(s["id"]) else "no"))
    return rows


if __name__ == "__main__":
    print(f"AEROTWIN_DATA = {AEROTWIN_DATA}")
    for sid, loc, status, ok in report():
        print(f"  {sid:14s} {loc:9s} {status:10s} available: {ok}")
    sys.exit(0)
