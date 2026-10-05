"""Fault classification with novelty detection and SHAP explanations.

Generalised from AeroTwin (sih26054_v2/twin/diagnose.py, class `Diagnoser` and `decide`).
Kept: random forest (or histogram gradient boosting) classifier, IsolationForest novelty score
trained on nominal rows, SHAP TreeExplainer attributions, "unknown" below a confidence floor
so a novel pattern is not forced into a known class. Dropped: AeroTwin's engine-specific
feature table, physics rules and cylinder / sensor localisation.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier

MIN_CONFIDENCE = 0.25


class Diagnoser:
    """model 'rf' (random forest) or 'hgb' (histogram gradient boosting).

    features:         columns used by the classifier
    novelty_features: columns used by the IsolationForest (default: all features)
    readable:         {feature: plain-language meaning} for explanations
    """

    def __init__(self, model="rf", novelty_features=None, readable=None, nominal="nominal"):
        self.model = model
        self.novelty_features = novelty_features
        self.readable = readable or {}
        self.nominal = nominal

    def fit(self, X, y, seed=0):
        y = np.asarray(y)
        self.features = list(X.columns)
        self.novelty_features = list(self.novelty_features or self.features)
        if self.model == "hgb":
            from sklearn.ensemble import HistGradientBoostingClassifier
            self.clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=31,
                                                      l2_regularization=1.0, class_weight="balanced",
                                                      random_state=seed)
        else:
            self.clf = RandomForestClassifier(n_estimators=160, min_samples_leaf=8, max_features="sqrt",
                                              class_weight="balanced_subsample", n_jobs=2, random_state=seed)
        self.clf.fit(X[self.features], y)
        nom = X[y == self.nominal] if np.any(y == self.nominal) else X
        self.iso = IsolationForest(n_estimators=200, contamination=0.005,
                                   random_state=seed).fit(nom[self.novelty_features])
        return self

    @property
    def classes_(self):
        return list(self.clf.classes_)

    def predict_proba(self, X):
        return pd.DataFrame(self.clf.predict_proba(X[self.features]), columns=self.clf.classes_, index=X.index)

    def novelty(self, X):
        return -self.iso.score_samples(X[self.novelty_features])

    def decide(self, pr, min_conf=MIN_CONFIDENCE):
        """Best non-nominal class of one probability row when confident, else 'unknown'.
        Returns (class, probability, basis)."""
        cand = pr.drop(self.nominal) if self.nominal in pr and len(pr) > 1 else pr
        cls = cand.idxmax()
        if float(pr[cls]) >= min_conf:
            return cls, float(pr[cls]), "classifier"
        return "unknown", float(pr[cls]), "none"

    def explain(self, X, cls, top=6):
        """Mean SHAP values of class `cls` over the rows of X, largest first."""
        import shap
        if not hasattr(self, "_expl"):
            self._expl = shap.TreeExplainer(self.clf)
        sv = self._expl.shap_values(X[self.features], check_additivity=False)
        k = self.classes_.index(cls)
        vals = sv[:, :, k] if np.ndim(sv) == 3 else sv[k]
        m = np.asarray(vals).mean(axis=0)
        order = np.argsort(-np.abs(m))[:top]
        xv = X[self.features].mean(axis=0)
        return [dict(feature=self.features[i], meaning=self.readable.get(self.features[i], self.features[i]),
                     shap=float(m[i]), value=float(xv.iloc[i])) for i in order]
