"""A2: pre-maintenance risk on real flights (NGAFID 2-day subset).

Question: from one flight's engine and electrical data, was this flight flown in the two days
BEFORE an unplanned maintenance action, or AFTER it? (NGAFID's benchmark task; the published
ConvMHSA reaches 76.0 % binary accuracy on the same 5 folds.) A model that separates the two
has learned what an aircraft that needs maintenance looks like.

Model: histogram gradient boosting on per-flight summaries (data_prep/ngafid_features.py), the
dataset's own 5 folds (train on four, test on the fifth). Issue class among 'before' flights:
AeroTwin's Diagnoser (random forest + SHAP + IsolationForest) over the grouped issue families.

    python -m airpower.health.premaint      -> outputs/health/premaint.json
"""
import json
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score

from airpower.health import artifacts
from airpower.config import OUTPUTS, PROCESSED
from airpower.health.diagnose import Diagnoser
from data_prep import sources

OUT = OUTPUTS / "health"
PUBLISHED_BINARY_ACCURACY = 0.760          # ConvMHSA, Yang et al. 2022 (as recorded in AeroTwin docs/DATA_PLAN.md)

FAMILY = {"baffle": "cooling (baffles)", "intake": "induction leak", "rocker": "oil leak (rocker cover)",
          "cylinder": "cylinder / compression", "run rough": "rough running", "oil cooler": "oil cooler",
          "failure": "engine failure", "overspeed": "propeller / overspeed", "idle": "idle / rpm",
          "seal": "engine seal / tube", "repair/reinstall": "engine repair", "pilot": "pilot-noticed"}


def family(label):
    for k, v in FAMILY.items():
        if k in label:
            return v
    return "other"


def load(src="ngafid_2days"):
    F = pd.read_parquet(PROCESSED / "ngafid" / f"{src}_flight_features.parquet")
    lab = sources.load_ngafid_labels(src)
    d = F.join(lab[["before_after", "fold", "label"]], how="inner")
    return d, [c for c in F.columns if c != "n_rows"]


def binary(d, cols, seed=0):
    rows, pred = [], pd.Series(index=d.index, dtype=float)
    for k in sorted(d["fold"].unique()):
        tr, te = d["fold"] != k, d["fold"] == k
        m = HistGradientBoostingClassifier(max_iter=500, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=30,
                                           l2_regularization=1.0, random_state=seed)
        m.fit(d.loc[tr, cols], d.loc[tr, "before_after"])
        p = m.predict_proba(d.loc[te, cols])[:, 1]
        pred[te] = p
        y = d.loc[te, "before_after"]
        rows.append(dict(fold=int(k), n=int(te.sum()), accuracy=float(accuracy_score(y, p >= 0.5)),
                         auc=float(roc_auc_score(y, p))))
    final = HistGradientBoostingClassifier(max_iter=500, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=30,
                                           l2_regularization=1.0, random_state=seed).fit(d[cols], d["before_after"])
    return rows, pred, final


def issue_class(d, cols, seed=0):
    b = d[d["before_after"] == 1].copy()
    b["family"] = b["label"].map(family)
    keep = b["family"].value_counts()
    b = b[b["family"].isin(keep[keep >= 100].index)]
    X = b[cols].fillna(b[cols].median())
    rows, y_all, p_all = [], [], []
    for k in sorted(b["fold"].unique()):
        tr, te = b["fold"] != k, b["fold"] == k
        dg = Diagnoser(model="hgb", nominal="__none__").fit(X[tr], b.loc[tr, "family"].to_numpy(), seed=seed)
        p = dg.predict_proba(X[te]).idxmax(axis=1)
        y_all += list(b.loc[te, "family"])
        p_all += list(p)
        rows.append(dict(fold=int(k), accuracy=float((p.to_numpy() == b.loc[te, "family"].to_numpy()).mean())))
    prior = b["family"].value_counts(normalize=True).max()
    return dict(classes=sorted(b["family"].unique()), flights=int(len(b)), folds=rows,
                accuracy=float(np.mean([r["accuracy"] for r in rows])),
                macro_f1=float(f1_score(y_all, p_all, average="macro")),
                majority_class_accuracy=float(prior),
                per_class_f1={c: float(v) for c, v in zip(sorted(set(y_all)),
                                                          f1_score(y_all, p_all, average=None, labels=sorted(set(y_all))))})


def main(argv=None):
    d, cols = load()
    rows, pred, final = binary(d, cols)
    acc = float(np.mean([r["accuracy"] for r in rows]))
    auc = float(np.mean([r["auc"] for r in rows]))
    print(f"before vs after maintenance: accuracy {acc:.3f} (published ConvMHSA {PUBLISHED_BINARY_ACCURACY:.3f}), "
          f"AUC {auc:.3f}; folds {[round(r['accuracy'], 3) for r in rows]}", flush=True)
    # which issue families are recognisable before the maintenance
    d = d.assign(p=pred, family=d["label"].map(family))
    by_fam = {f: dict(flights=int(len(g)), auc_vs_after=float(roc_auc_score(
        np.r_[np.ones((g["before_after"] == 1).sum()), np.zeros((d["before_after"] == 0).sum())],
        np.r_[g.loc[g["before_after"] == 1, "p"], d.loc[d["before_after"] == 0, "p"]])))
        for f, g in d[d["before_after"] == 1].groupby("family") if len(g) >= 50}
    ic = issue_class(d, cols)
    print(f"issue family among 'before' flights: accuracy {ic['accuracy']:.3f} (majority class {ic['majority_class_accuracy']:.3f}), "
          f"macro-F1 {ic['macro_f1']:.3f}", flush=True)
    imp = None
    try:
        from sklearn.inspection import permutation_importance
        sub = d.sample(3000, random_state=0)
        pi = permutation_importance(final, sub[cols], sub["before_after"], n_repeats=3, random_state=0, scoring="roc_auc")
        imp = sorted(zip(cols, pi.importances_mean), key=lambda x: -x[1])[:12]
    except Exception as e:                     # importance is informative only
        print("importance skipped:", e)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "models").mkdir(parents=True, exist_ok=True)
    artifacts.save(dict(model=final, cols=cols), OUTPUTS / "models" / "premaint_ngafid.joblib")
    res = dict(dataset="NGAFID 2-day subset (11,446 real flights, Cessna 172S / Lycoming IO-360)", task="before vs after unplanned maintenance",
               folds=rows, accuracy=acc, auc=auc, published_convmhsa_accuracy=PUBLISHED_BINARY_ACCURACY,
               auc_by_issue_family=by_fam, issue_family=ic,
               top_features_auc_drop=[dict(feature=c, auc_drop=round(float(v), 4)) for c, v in imp] if imp else None)
    (OUT / "premaint.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
