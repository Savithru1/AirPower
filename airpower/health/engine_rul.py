"""A1: engine remaining useful life from trend data (NASA C-MAPSS), with calibrated intervals.

Method
* Sensors: constant channels dropped; FD002 / FD004 (six operating conditions) normalised per
  condition (conditions = rounded operating settings), FD001 / FD003 globally.
* Features at each cycle, over the last WINDOW cycles: smoothed level, mean, standard deviation
  and least-squares slope of every sensor, plus the number of cycles observed.
* Target: RUL capped at CAP cycles (the usual piecewise-linear target: early in life the data
  cannot tell 200 from 300 cycles left).
* Model: histogram gradient boosting (scikit-learn; no deep-learning dependency).
* Interval: split conformal on calibration engines held out of training, Mondrian by predicted
  RUL band, so the interval widens where predictions are less certain. Nominal 90 %.
* Mid-life starts: training and calibration also use copies of each trajectory that start
  part-way through life (age known from the records, short trend history), which is how most
  fleet engines first appear in the data. Without them the fleet intervals covered 65 %.
* Training data: for FD001 / FD003 only the units data_prep/cmapss.py marks 'train'; the
  'fleet' units are reserved for FleetSim and never seen here.
* Scoring: official test sets (prediction at each test engine's last cycle vs RUL_FD00x):
  RMSE, NASA asymmetric score, interval coverage.

    python -m airpower.health.engine_rul        # all four subsets -> outputs/health/engine_rul.json
"""
import json
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from airpower.health import artifacts
from airpower.config import OUTPUTS
from data_prep import cmapss

WINDOW = 30
CAP = 125
LATE = 100000          # unit-id offset of the mid-life-start training copies
ALPHA = 0.10
BANDS = (0, 40, 80, 1e9)
OUT = OUTPUTS / "health"
MODELS = OUTPUTS / "models"
REFERENCE = {
    "FD001": "published RMSE: CNN 18.4 (Babu 2016), LSTM 16.1 (Zheng 2017), deep CNN 12.6 (Li 2018)",
    "FD002": "published RMSE: CNN 30.3 (Babu 2016), LSTM 24.5 (Zheng 2017), deep CNN 22.4 (Li 2018)",
    "FD003": "published RMSE: CNN 19.8 (Babu 2016), LSTM 16.2 (Zheng 2017), deep CNN 12.6 (Li 2018)",
    "FD004": "published RMSE: CNN 29.2 (Babu 2016), LSTM 28.2 (Zheng 2017), deep CNN 23.3 (Li 2018)",
}


def nasa_score(d):
    """PHM08 score: late predictions (d > 0) cost more than early ones."""
    d = np.asarray(d, float)
    return float(np.sum(np.where(d < 0, np.exp(-d / 13.0) - 1, np.exp(d / 10.0) - 1)))


class EngineRUL:
    def __init__(self, subset):
        self.subset = subset
        self.multi = subset in ("FD002", "FD004")

    # ------------------------------------------------------------------ preprocessing
    def _cond(self, d):
        return (d["setting1"].round(0).astype(int).astype(str) + "_" + d["setting2"].round(2).astype(str) + "_" +
                d["setting3"].round(0).astype(int).astype(str))

    def fit_norm(self, d):
        self.sensors = [s for s in cmapss.SENSORS if d[s].std() > 1e-6]
        if self.multi:
            c = self._cond(d)
            self.stats = {k: (g[self.sensors].mean(), g[self.sensors].std().replace(0, 1.0)) for k, g in d.groupby(c)}
            # some sensors are constant within a condition but not across
        else:
            self.stats = {"all": (d[self.sensors].mean(), d[self.sensors].std().replace(0, 1.0))}
        return self

    def normalise(self, d):
        d = d.copy()
        d[self.sensors] = d[self.sensors].astype(float)
        if self.multi:
            c = self._cond(d)
            for k, (mu, sd) in self.stats.items():
                m = c == k
                d.loc[m, self.sensors] = (d.loc[m, self.sensors] - mu) / sd
        else:
            mu, sd = self.stats["all"]
            d[self.sensors] = (d[self.sensors] - mu) / sd
        return d

    def features(self, d):
        """Rolling-window features per (unit, cycle). d must be normalised and sorted."""
        d = d.sort_values(["unit", "cycle"])
        out = [d[["unit", "cycle"]].reset_index(drop=True)]
        g = d.groupby("unit")
        k = np.arange(WINDOW, dtype=float)
        feats = {}
        for s in self.sensors:
            x = d[s]
            r = g[s].rolling(WINDOW, min_periods=1)
            feats[f"{s}_mean"] = r.mean().reset_index(level=0, drop=True)
            feats[f"{s}_std"] = r.std().reset_index(level=0, drop=True)
            feats[f"{s}_ewm"] = g[s].transform(lambda v: v.ewm(alpha=0.1).mean())
            # slope over the window: cov(t, x) / var(t) with t = 0..n-1
            n = g[s].rolling(WINDOW, min_periods=2).count().reset_index(level=0, drop=True)
            sx = g[s].rolling(WINDOW, min_periods=2).sum().reset_index(level=0, drop=True)
            idx = d.groupby("unit").cumcount().astype(float)
            tx = (x * idx)
            stx = tx.groupby(d["unit"]).rolling(WINDOW, min_periods=2).sum().reset_index(level=0, drop=True)
            st = idx.groupby(d["unit"]).rolling(WINDOW, min_periods=2).sum().reset_index(level=0, drop=True)
            stt = (idx ** 2).groupby(d["unit"]).rolling(WINDOW, min_periods=2).sum().reset_index(level=0, drop=True)
            feats[f"{s}_slope"] = (n * stx - st * sx) / (n * stt - st ** 2).replace(0, np.nan)
        F = pd.DataFrame(feats).reset_index(drop=True)
        # cycles since the start of the engine's life: equals the row count on C-MAPSS; in the fleet it
        # comes from the records (life card hours), NaN when the age is unknown (handled natively by HGB)
        F["cycles_seen"] = (d["cycles_seen"].to_numpy() if "cycles_seen" in d
                            else d.groupby("unit").cumcount().to_numpy() + 1)
        return pd.concat([out[0], F], axis=1)

    # ------------------------------------------------------------------ fit
    @staticmethod
    def late_starts(d, copies, rng, lo=0.15, hi=0.75):
        """Copies of each trajectory whose record starts mid-life (unit id + k * LATE). In a fleet most
        engines are already part-way through their life when their trend data starts, so the window
        features see a short history at a high age; the C-MAPSS training units always start at cycle 1."""
        d = d.assign(cycles_seen=d["cycle"].astype(float))
        out = [d]
        for k in range(1, copies + 1):
            for u, g in d.groupby("unit"):
                o = int(rng.uniform(lo, hi) * len(g))
                if len(g) - o >= 10:
                    out.append(g.iloc[o:].assign(unit=u + k * LATE))
        return pd.concat(out, ignore_index=True)

    def fit(self, train, calib_frac=0.2, seed=0, copies=2):
        rng = np.random.default_rng(seed)
        units = np.array(sorted(train["unit"].unique()))
        cal = set(rng.choice(units, size=max(3, int(calib_frac * len(units))), replace=False))
        self.fit_norm(train[~train["unit"].isin(cal)])
        d = self.late_starts(train, copies, rng).sort_values(["unit", "cycle"]).reset_index(drop=True)
        F = self.features(self.normalise(d))
        y = np.minimum(d["rul"].to_numpy(), CAP)
        self.cols = [c for c in F.columns if c not in ("unit", "cycle")]
        tr = ~(F["unit"] % LATE).isin(cal).to_numpy()
        # an engine whose age is not in the records reaches the model with cycles_seen = NaN: train on
        # a copy of every row with the age masked, so the model learns that case instead of guessing
        Fm = F.copy()
        Fm["cycles_seen"] = np.nan
        FF = pd.concat([F, Fm], ignore_index=True)
        yy = np.r_[y, y]
        tt = np.r_[tr, tr]
        known = np.r_[np.ones(len(F), bool), np.zeros(len(F), bool)]
        self.model = HistGradientBoostingRegressor(max_iter=600, learning_rate=0.05, max_leaf_nodes=31,
                                                   min_samples_leaf=40, l2_regularization=1.0, random_state=seed)
        self.model.fit(FF.loc[tt, self.cols], yy[tt])
        # conformal calibration on held-out engines, at every cycle (as a truncation point), Mondrian by
        # predicted RUL band and by whether the age is known
        pc = self.model.predict(FF.loc[~tt, self.cols])
        res = np.abs(yy[~tt] - pc)
        kn = known[~tt]
        self.q = {}
        for age_known in (True, False):
            for lo, hi in zip(BANDS[:-1], BANDS[1:]):
                m = (pc >= lo) & (pc < hi) & (kn == age_known)
                r = np.sort(res[m]) if m.sum() >= 30 else np.sort(res[kn == age_known])
                k = min(len(r) - 1, int(np.ceil((len(r) + 1) * (1 - ALPHA))) - 1)
                self.q[(age_known, lo, hi)] = float(r[k])
        self.n_train_units, self.n_cal_units = int(len(units) - len(cal)), int(len(cal))
        return self

    # ------------------------------------------------------------------ predict
    def predict_frame(self, d):
        """d: raw C-MAPSS-style rows (unit, cycle, settings, sensors) -> per-row prediction + interval."""
        F = self.features(self.normalise(d))
        p = np.clip(self.model.predict(F[self.cols]), 0, None)
        known = F["cycles_seen"].notna().to_numpy()
        half = np.array([next(v for (kn, lo, hi), v in self.q.items() if kn == k and lo <= x < hi) for x, k in zip(p, known)])
        return F[["unit", "cycle"]].assign(rul=p, lo=np.clip(p - half, 0, None), hi=p + half)

    def explain(self, d, top=5):
        """Which trend features drive the prediction for the last row of each unit (permutation-free:
        the model's built-in split gains are not exposed for HGB, so a local perturbation is used)."""
        F = self.features(self.normalise(d)).groupby("unit").tail(1)
        X = F[self.cols].to_numpy(dtype=float)
        base = self.model.predict(F[self.cols])
        feats = [j for j, c in enumerate(self.cols) if c != "cycles_seen"]
        # one batched prediction: every unit x every feature moved to the healthy (normalised) mean
        P = np.repeat(X, len(feats), axis=0)
        P[np.arange(len(P)), np.tile(feats, len(X))] = 0.0
        moved = self.model.predict(pd.DataFrame(P, columns=self.cols)).reshape(len(X), len(feats))
        out = []
        for i in range(len(X)):
            eff = base[i] - moved[i]
            order = np.argsort(-np.abs(eff))[:top]
            out.append([dict(feature=self.cols[feats[k]], effect_cycles=round(float(eff[k]), 1)) for k in order])
        return out


def train_units(subset):
    d = cmapss.load(subset, "train")
    if subset in cmapss.FLEET_SUBSETS:
        d = d[d["unit"].isin(cmapss.split()["subsets"][subset]["train"])]
    return d


def evaluate(subset):
    m = EngineRUL(subset).fit(train_units(subset))
    te = cmapss.load(subset, "test")
    p = m.predict_frame(te)
    last = p.groupby("unit").tail(1).merge(te.groupby("unit")["rul"].last().rename("true"), on="unit")
    d = last["rul"] - last["true"]
    d_cap = last["rul"] - np.minimum(last["true"], CAP)
    cover = (last["lo"] <= np.minimum(last["true"], CAP)) & (np.minimum(last["true"], CAP) <= last["hi"])
    # all-cycle coverage on the test set (every truncation point), capped truth
    full = p.merge(te[["unit", "cycle", "rul"]].rename(columns={"rul": "true"}), on=["unit", "cycle"])
    tc = np.minimum(full["true"], CAP)
    cov_all = float(((full["lo"] <= tc) & (tc <= full["hi"])).mean())
    MODELS.mkdir(parents=True, exist_ok=True)
    artifacts.save(m, MODELS / f"engine_rul_{subset}.joblib")
    return dict(subset=subset, train_units=m.n_train_units, calibration_units=m.n_cal_units, test_units=int(len(last)),
                rmse=float(np.sqrt(np.mean(d ** 2))), rmse_true_capped=float(np.sqrt(np.mean(d_cap ** 2))),
                mae=float(np.mean(np.abs(d))), nasa_score=nasa_score(d),
                interval_nominal=1 - ALPHA, coverage_last_cycle=float(cover.mean()), coverage_all_cycles=cov_all,
                mean_interval_width=float((last["hi"] - last["lo"]).mean()),
                conformal_half_widths={f"{'age known' if kn else 'age unknown'} {lo}-{hi if hi < 1e9 else 'inf'}": round(v, 1)
                                       for (kn, lo, hi), v in m.q.items()},
                reference=REFERENCE[subset])


def main(argv=None):
    OUT.mkdir(parents=True, exist_ok=True)
    res = []
    for s in cmapss.SUBSETS:
        r = evaluate(s)
        res.append(r)
        print(f"{s}: RMSE {r['rmse']:.1f} (true RUL capped {r['rmse_true_capped']:.1f}), NASA score {r['nasa_score']:.0f}, "
              f"90 % interval coverage {r['coverage_last_cycle']:.0%} (all cycles {r['coverage_all_cycles']:.0%}), "
              f"width {r['mean_interval_width']:.0f}  | {r['reference']}", flush=True)
    (OUT / "engine_rul.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    # run through the importable module so pickled models reference airpower.health.engine_rul.EngineRUL
    from airpower.health import engine_rul as _m
    sys.exit(_m.main())
