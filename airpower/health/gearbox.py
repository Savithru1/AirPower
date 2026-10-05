"""A3: gearbox / bearing fault detection and classification on real vibration (CWRU, SEU).

Features: AeroTwin's edge DSP on 0.5 s windows (RMS, kurtosis, crest factor, envelope-spectrum
kurtosis). Validation is leave-one-operating-condition-out: train on all loads / speeds but
one, test on the unseen one. Windows cut from the same recording are never split between
train and test (random window splits leak and inflate CWRU results).

Two questions, because they need different things from HUMS:
* detection: healthy vs any fault (what raises a HUMS alert);
* classification: which fault (what the maintainer is told to look for).
CWRU classes are fault locations (ball, inner race, outer race); the 0.021 in. defects exist only
at 0 hp, so severity is not a class. SEU classes are gear tooth faults.

    python -m airpower.health.gearbox       -> outputs/health/gearbox.json
"""
import json
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import f1_score, roc_auc_score

from airpower.health import artifacts
from airpower.config import OUTPUTS
from data_prep import sources

FEATS = ["Vib_RMS", "Vib_Kurtosis", "crest", "env_spec_kurt"]
LOC = {"B": "ball", "IR": "inner race", "OR": "outer race"}


def classes(w):
    w = w.copy()
    w["cls"] = np.where(w["label"] == "healthy", "healthy",
                        np.where(w["dataset"] == "cwru_bearing", w["label"].str.extract(r"^([A-Z]+)")[0].map(LOC), w["label"]))
    # log features are better behaved across loads
    for f in ("Vib_RMS",):
        w[f + "_log"] = np.log(w[f])
    return w


def loco(w, feats):
    rows, y_all, p_all, det = [], [], [], []
    for cond in sorted(w["condition"].unique()):
        tr, te = w["condition"] != cond, w["condition"] == cond
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=10,
                                           class_weight="balanced", random_state=0)
        m.fit(w.loc[tr, feats], w.loc[tr, "cls"])
        p = m.predict(w.loc[te, feats])
        pr = pd.DataFrame(m.predict_proba(w.loc[te, feats]), columns=m.classes_, index=w.index[te])
        y = w.loc[te, "cls"]
        healthy_p = pr["healthy"] if "healthy" in pr else 0.0
        auc = roc_auc_score((y != "healthy").astype(int), 1 - healthy_p) if y.nunique() > 1 else None
        rows.append(dict(held_out=cond, windows=int(te.sum()), accuracy=float((p == y.to_numpy()).mean()),
                         detection_auc=None if auc is None else float(auc)))
        y_all += list(y)
        p_all += list(p)
    return rows, float(f1_score(y_all, p_all, average="macro"))


def baseline_normalised(g, feats, n_ref=40, seed=0):
    """Each condition's features relative to a healthy reference at that condition, as a serial's
    own healthy history provides in service (acceptance run, first flights). The reference
    windows are removed from the evaluation set so they are never scored."""
    rng = np.random.default_rng(seed)
    out = []
    for cond, c in g.groupby("condition"):
        h = c.index[c["cls"] == "healthy"]
        ref = rng.choice(h, size=min(n_ref, len(h) // 2), replace=False)
        mu, sd = c.loc[ref, feats].mean(), c.loc[ref, feats].std().replace(0, 1.0)
        z = c.drop(index=ref).copy()
        z[feats] = (z[feats] - mu) / sd
        out.append(z)
    return pd.concat(out)


def main(argv=None):
    w = classes(sources.load_vibration_windows())
    feats = ["Vib_RMS_log", "Vib_Kurtosis", "crest", "env_spec_kurt"]
    res = {}
    for ds, g in w.groupby("dataset"):
        res[ds] = {}
        for variant, data in (("raw_features", g), ("baseline_normalised", baseline_normalised(g, feats))):
            rows, mf1 = loco(data, feats)
            r = dict(classes=sorted(g["cls"].unique()), windows=int(len(data)), held_out_conditions=rows,
                     accuracy=float(np.mean([x["accuracy"] for x in rows])),
                     detection_auc=float(np.nanmean([x["detection_auc"] for x in rows if x["detection_auc"] is not None])),
                     macro_f1=mf1)
            res[ds][variant] = r
            print(f"{ds} [{variant}]: leave-one-condition-out accuracy {r['accuracy']:.3f}, macro-F1 {mf1:.3f}, "
                  f"healthy-vs-fault AUC {r['detection_auc']:.3f}; " +
                  ", ".join(f"{x['held_out']} {x['accuracy']:.2f}" for x in rows), flush=True)
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=10,
                                           class_weight="balanced", random_state=0).fit(g[feats], g["cls"])
        (OUTPUTS / "models").mkdir(parents=True, exist_ok=True)
        artifacts.save(dict(model=m, feats=feats), OUTPUTS / "models" / f"vibration_{ds}.joblib")
    (OUTPUTS / "health").mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "health" / "gearbox.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
