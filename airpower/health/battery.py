"""A4: battery remaining useful life from capacity fade (NASA Ames Li-ion cells).

End of life: state of health (capacity / initial) 0.75. Cells that reach it with at least 20
cycles of history are scored leave-one-cell-out at 40 / 60 / 80 % of their life, as AeroTwin's
benchmark did (tools/rul_benchmarks.py), so the numbers are comparable:
AeroTwin particle filter 0.07, per-cell power law 0.19 alpha-lambda accuracy (alpha 0.2).

Methods
* trend    - robust linear extrapolation of the last 30 cycles' state of health (Theil-Sen);
* gbm      - gradient boosting on (state of health, recent slopes, curvature, cycle, ambient,
             current) trained on the other cells' histories, target = cycles to end of life.
Intervals: split conformal on the relative error of the other cells (leave-one-cell-out), 90 %.

NASA cells regain capacity after rest periods, so the fade is noisy and non-monotone; with nine
qualifying cells any result here is indicative, not a validated model.

    python -m airpower.health.battery       -> outputs/health/battery.json
"""
import json
import sys

import joblib
import numpy as np
import pandas as pd
from scipy.stats import theilslopes
from sklearn.ensemble import HistGradientBoostingRegressor

from airpower.health import artifacts
from airpower.config import OUTPUTS
from data_prep import sources

EOL = 0.75
AEROTWIN = {"particle_filter_accelerating": 0.074, "per_cell_power_law_bootstrap": 0.185}


def cells():
    d = sources.load_battery_cycles()
    d = d[d["usable"]].copy()
    d["i_cell"] = d.groupby("cell")["i_mean"].transform("median")
    out = {}
    for c, g in d.groupby("cell"):
        g = g.sort_values("cycle").reset_index(drop=True)
        g["soh_s"] = g["soh"].rolling(3, min_periods=1).median()
        below = np.flatnonzero(g["soh_s"].to_numpy() <= EOL)
        if len(below) and below[0] >= 20:
            g["eol_cycle"] = int(g["cycle"].iloc[below[0]])
            out[c] = g
    return out


def feats(g, upto):
    h = g[g["cycle"] <= upto]
    s = h["soh_s"].to_numpy()
    n = h["cycle"].to_numpy(float)

    def slope(k):
        k = min(k, len(s))
        return np.polyfit(n[-k:], s[-k:], 1)[0] if k >= 3 else 0.0

    curv = np.polyfit(n[-30:], s[-30:], 2)[0] if len(s) >= 10 else 0.0
    return dict(soh=s[-1], soh0=float(np.median(s[:3])), slope10=slope(10), slope30=slope(30), slope_all=slope(len(s)),
                curvature=curv, cycle=n[-1], ambient=float(h["ambient_c"].iloc[0]), current=float(h["i_cell"].iloc[0]),
                gap=s[-1] - EOL)


def trend_rul(g, upto):
    h = g[g["cycle"] <= upto].tail(30)
    sl, ic, *_ = theilslopes(h["soh_s"], h["cycle"])
    if sl >= 0:
        return 500.0
    return float(max((EOL - ic) / sl - upto, 0.0))


def training_rows(cs, exclude):
    rows = []
    for c, g in cs.items():
        if c == exclude:
            continue
        e = g["eol_cycle"].iloc[0]
        for upto in g.loc[(g["cycle"] >= 10) & (g["cycle"] < e), "cycle"]:
            f = feats(g, upto)
            f["y"] = e - upto
            rows.append(f)
    return pd.DataFrame(rows)


def main(argv=None):
    cs = cells()
    preds = []
    for c, g in cs.items():
        tr = training_rows(cs, c)
        X = tr.drop(columns="y")
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=10,
                                          random_state=0).fit(X, tr["y"])
        e = g["eol_cycle"].iloc[0]
        for frac in (0.4, 0.6, 0.8):
            upto = int(round(frac * e))
            upto = int(g.loc[g["cycle"] <= upto, "cycle"].max())
            f = pd.DataFrame([feats(g, upto)])
            preds.append(dict(cell=c, frac=frac, true=e - upto, gbm=float(max(m.predict(f[X.columns])[0], 0)),
                              trend=trend_rul(g, upto)))
    P = pd.DataFrame(preds)
    res = dict(dataset=f"NASA Ames Li-ion cells reaching SoH {EOL}", cells=int(P["cell"].nunique()), predictions=len(P),
               aerotwin_alpha_lambda_a02=AEROTWIN)
    for meth in ("trend", "gbm"):
        rel = (P[meth] - P["true"]) / P["true"]
        # leave-one-cell-out conformal on relative error: quantile from the OTHER cells
        cover, widths = [], []
        for i, r in P.iterrows():
            others = np.abs(rel[P["cell"] != r["cell"]])
            q = float(np.quantile(others, 0.9))
            lo, hi = r[meth] / (1 + q), r[meth] / max(1 - q, 0.05)
            cover.append(lo <= r["true"] <= hi)
            widths.append(hi - lo)
        res[meth] = dict(alpha_lambda_accuracy_a02=float(np.mean(np.abs(rel) <= 0.2)),
                         alpha_lambda_accuracy_a03=float(np.mean(np.abs(rel) <= 0.3)),
                         mae_cycles=float(np.mean(np.abs(P[meth] - P["true"]))),
                         interval90_coverage=float(np.mean(cover)), mean_interval_width_cycles=float(np.mean(widths)),
                         by_fraction_a02={str(f): float(np.mean(np.abs(rel[P["frac"] == f]) <= 0.2)) for f in (0.4, 0.6, 0.8)})
        print(f"{meth}: alpha-lambda(0.2) {res[meth]['alpha_lambda_accuracy_a02']:.2f}, (0.3) {res[meth]['alpha_lambda_accuracy_a03']:.2f}, "
              f"MAE {res[meth]['mae_cycles']:.1f} cycles, 90 % interval coverage {res[meth]['interval90_coverage']:.2f}", flush=True)
    print(f"AeroTwin reference (alpha 0.2): {AEROTWIN}")
    full = training_rows(cs, None)
    m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=10,
                                      random_state=0).fit(full.drop(columns="y"), full["y"])
    (OUTPUTS / "models").mkdir(parents=True, exist_ok=True)
    artifacts.save(dict(model=m, cols=list(full.drop(columns="y").columns), eol=EOL), OUTPUTS / "models" / "battery_rul.joblib")
    (OUTPUTS / "health").mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "health" / "battery.json").write_text(json.dumps(res, indent=1))
    P.to_csv(OUTPUTS / "health" / "battery_predictions.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
