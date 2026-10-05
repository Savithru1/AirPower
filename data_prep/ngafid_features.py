"""NGAFID per-flight features for the pre-maintenance risk model (A2).

Reads AeroTwin's harmonised 1 Hz flights (contract-v2 names, SI units) part by part and
summarises each flight: whole-flight statistics per channel, cruise statistics (steady
level flight at power), cylinder-to-cylinder spreads and deviations, and variability measures
that rough running or leaks would raise. Output: data/processed/ngafid/<src>_flight_features.parquet

    python -m data_prep.ngafid_features [--src ngafid_2days]
"""
import argparse
import sys
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from airpower.config import PROCESSED
from data_prep import sources

CORE = ["E1_RPM", "E1_Fuel_Flow", "E1_Oil_Temp", "E1_Oil_Pressure", "Bus_Voltage", "Batt_Current", "x_Normal_Accel",
        "IAS", "Altitude", "Ambient_Temp"]
CHT = [f"E1_CHT_Cyl{k}" for k in range(1, 5)]
EGT = [f"E1_EGT_Cyl{k}" for k in range(1, 5)]


def _q(p):
    f = lambda s: s.quantile(p)        # noqa: E731
    f.__name__ = f"p{int(p * 100):02d}"
    return f


def features(df):
    g = df.groupby("flight_id")
    out = [g.size().rename("n_rows")]
    for c in CORE + CHT + EGT:
        a = g[c].agg(["mean", "std", "max", "min"])
        a.columns = [f"{c}__{s}" for s in a.columns]
        out.append(a)
    # steady cruise: at power, flying, level
    cr = df[(df["E1_RPM"] > 2200) & (df["IAS"] > 70) & (df["Vertical_Speed"].abs() < 300)].copy()
    cr["cht_spread"] = cr[CHT].max(axis=1) - cr[CHT].min(axis=1)
    cr["egt_spread"] = cr[EGT].max(axis=1) - cr[EGT].min(axis=1)
    cht_m, egt_m = cr[CHT].mean(axis=1), cr[EGT].mean(axis=1)
    for k in range(4):
        cr[f"cht_dev{k + 1}"] = cr[CHT[k]] - cht_m
        cr[f"egt_dev{k + 1}"] = cr[EGT[k]] - egt_m
    cr["oilp_per_rpm"] = cr["E1_Oil_Pressure"] / cr["E1_RPM"] * 1000
    cr["ff_per_rpm"] = cr["E1_Fuel_Flow"] / cr["E1_RPM"] * 1000
    cr["cht_minus_oat"] = cht_m - cr["Ambient_Temp"]
    cr["oilt_minus_oat"] = cr["E1_Oil_Temp"] - cr["Ambient_Temp"]
    gc = cr.groupby("flight_id")
    cols = ["E1_RPM", "E1_Fuel_Flow", "E1_Oil_Temp", "E1_Oil_Pressure", "cht_spread", "egt_spread", "oilp_per_rpm",
            "ff_per_rpm", "cht_minus_oat", "oilt_minus_oat"] + [f"cht_dev{k}" for k in range(1, 5)] + \
        [f"egt_dev{k}" for k in range(1, 5)] + EGT
    a = gc[cols].agg(["mean", "std"])
    a.columns = [f"cruise_{c}__{s}" for c, s in a.columns]
    out.append(a)
    out.append(gc.size().rename("cruise_rows"))
    return pd.concat(out, axis=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="ngafid_2days")
    a = ap.parse_args(argv)
    flights_dir = sources.paths(a.src)[1]
    parts = sorted(flights_dir.glob("*.parquet"))
    cols = ["flight_id", "Vertical_Speed"] + CORE + CHT + EGT
    t0, res = time.time(), []
    for i, p in enumerate(parts):
        df = pq.read_table(p, columns=cols).to_pandas()
        res.append(features(df))
        print(f"  {i + 1}/{len(parts)} {p.name}: {df['flight_id'].nunique()} flights, {time.time() - t0:.0f} s", flush=True)
    F = pd.concat(res)
    F = F[~F.index.duplicated()]
    out = PROCESSED / "ngafid" / f"{a.src}_flight_features.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    F.replace([np.inf, -np.inf], np.nan).to_parquet(out)
    print(f"-> {out}: {F.shape[0]} flights x {F.shape[1]} features ({time.time() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
