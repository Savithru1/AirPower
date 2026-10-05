"""A7: defect text -> ATA chapter, trained and scored on real FAA Service Difficulty Reports.

Label: the ATA chapter = first two digits of the report's JASC code. Split by time: train on
2020-2023, test on 2024-2025 (the model is judged on reports written after it was trained).
Many write-ups cite manual references ("PER AMM 32-45-05"), which give the chapter away; a
pilot's defect entry would not have them, so the model is also trained and scored with every
reference-like token masked. Text is normalised with the integration glossary (abbreviations).

Model: TF-IDF of word 1-2 grams + linear SVM (one-vs-rest). Confidence: margin of the best class,
reported as an accuracy-coverage curve (how accurate the model is on the share of reports it is
most confident about), which is how it would be used to auto-fill a missing ATA chapter.

    python -m airpower.nlp.ata         -> outputs/nlp/ata.json, outputs/models/ata_sdr.joblib
"""
import json
import re
import sys
import time

import joblib
import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import f1_score
from sklearn.svm import LinearSVC

from airpower.health import artifacts
from airpower.config import OUTPUTS, RAW
from airpower.ingest.resolve import normalize_text

TRAIN_YEARS = range(2020, 2024)
TEST_YEARS = range(2024, 2026)
MIN_TRAIN = 100
REF = re.compile(r"\b(?:AMM|IPC|SRM|CMM|FIM|TSM|MM|ATA|WDM|SSM|TASK|CH|CHAPTER)\b[\s#:.]*[\dA-Z./-]*"
                 r"|\b\d{2}[-\s.]\d{2}(?:[-\s.]\d{2,3})*(?:[-\s.]\w+)?\b")


def load(years):
    parts = []
    for y in years:
        d = pd.read_csv(RAW / "faa_sdrs" / f"SDR-{y}.csv", dtype=str, encoding_errors="replace",
                        usecols=["JASCCode", "Discrepancy", "AircraftMake", "AircraftModel", "PartName"])
        d["year"] = y
        parts.append(d)
    d = pd.concat(parts, ignore_index=True)
    d = d[d["JASCCode"].str.fullmatch(r"\d{4}", na=False) & d["Discrepancy"].notna()]
    d["ata"] = d["JASCCode"].str[:2]
    return d


def prep(s, mask):
    s = s.fillna("")
    if mask:
        s = s.str.replace(REF, " ref ", regex=True)
    return s.map(normalize_text)


def train(texts, y):
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=3, max_features=250_000, sublinear_tf=True,
                          token_pattern=r"(?u)\b[a-z][a-z0-9/]+\b")
    X = vec.fit_transform(texts)
    clf = LinearSVC(C=0.5).fit(X, y)
    return vec, clf


def evaluate(vec, clf, texts, y):
    S = clf.decision_function(vec.transform(texts))
    order = np.argsort(-S, axis=1)
    cls = clf.classes_
    top1 = cls[order[:, 0]]
    top3 = np.array([y_i in cls[o[:3]] for y_i, o in zip(y, order)])
    margin = S[np.arange(len(S)), order[:, 0]] - S[np.arange(len(S)), order[:, 1]]
    curve = {}
    for cov in (1.0, 0.8, 0.6, 0.4):
        k = max(1, int(cov * len(S)))
        idx = np.argsort(-margin)[:k]
        curve[f"{int(cov * 100)}%"] = float((top1[idx] == np.asarray(y)[idx]).mean())
    per = f1_score(y, top1, average=None, labels=cls)
    return dict(n=int(len(y)), top1_accuracy=float((top1 == np.asarray(y)).mean()), top3_accuracy=float(top3.mean()),
                macro_f1=float(f1_score(y, top1, average="macro")), weighted_f1=float(f1_score(y, top1, average="weighted")),
                accuracy_at_coverage=curve,
                per_chapter_f1={c: round(float(f), 3) for c, f in sorted(zip(cls, per), key=lambda x: -x[1])}), top1


def transfer_to_fleet(vec, clf, mask):
    """Score the SDR-trained model on the integrated store's defect texts that carry an ATA chapter."""
    try:
        from sqlalchemy import text as sql
        from airpower.config import db_url
        from airpower.model import schema as S
        d = pd.read_sql(sql("select text, ata, ata_source from defect where ata_source = 'record' and duplicate_of is null"),
                        S.engine(db_url()))
    except Exception as e:                       # store not loaded
        return dict(skipped=str(e))
    y = d["ata"].astype(int).astype(str).str.zfill(2)
    known = y.isin(clf.classes_)
    p = clf.predict(vec.transform(prep(d.loc[known, "text"], mask)))
    return dict(defects=int(known.sum()), top1_accuracy=float((p == y[known].to_numpy()).mean()),
                note="FleetSim logbook text is template-based with abbreviations; a domain-transfer check, not a benchmark")


def citation_share(d):
    """Share of reports that cite their own ATA chapter as 'NN-xx' (what masking guards against)."""
    return float(np.mean([bool(re.search(r"\b" + a + r"[-\s.]\d{2}", t)) for a, t in zip(d["ata"], d["Discrepancy"])]))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if "--citation-share-only" in argv:          # add the figure to an existing result without retraining
        path = OUTPUTS / "nlp" / "ata.json"
        res = json.loads(path.read_text())
        res["share_citing_own_chapter"] = citation_share(load(TEST_YEARS))
        path.write_text(json.dumps(res, indent=1))
        print(f"share citing own chapter: {res['share_citing_own_chapter']:.3f}")
        return 0
    t0 = time.time()
    tr, te = load(TRAIN_YEARS), load(TEST_YEARS)
    counts = tr["ata"].value_counts()
    keep = set(counts[counts >= MIN_TRAIN].index)
    tr = tr[tr["ata"].isin(keep)]
    te_all = len(te)
    te = te[te["ata"].isin(keep)]
    res = dict(source="FAA Service Difficulty Reports", train_years=list(TRAIN_YEARS), test_years=list(TEST_YEARS),
               train_reports=int(len(tr)), test_reports=int(len(te)), test_reports_in_unseen_chapters=int(te_all - len(te)),
               chapters=sorted(keep), majority_class_accuracy=float((te["ata"] == counts.index[0]).mean()),
               share_citing_own_chapter=citation_share(te))
    print(f"train {len(tr)} reports, test {len(te)} reports, {len(keep)} chapters ({time.time() - t0:.0f} s)", flush=True)
    for mask in (False, True):
        name = "references_masked" if mask else "full_text"
        vec, clf = train(prep(tr["Discrepancy"], mask), tr["ata"].to_numpy())
        r, _ = evaluate(vec, clf, prep(te["Discrepancy"], mask), te["ata"].to_numpy())
        r["fleet_transfer"] = transfer_to_fleet(vec, clf, mask)
        res[name] = r
        print(f"{name}: top-1 {r['top1_accuracy']:.3f}, top-3 {r['top3_accuracy']:.3f}, macro-F1 {r['macro_f1']:.3f}, "
              f"accuracy on the 60 % most confident {r['accuracy_at_coverage']['60%']:.3f}; fleet transfer "
              f"{r['fleet_transfer'].get('top1_accuracy', 'n/a')} ({time.time() - t0:.0f} s)", flush=True)
        if mask:
            (OUTPUTS / "models").mkdir(parents=True, exist_ok=True)
            artifacts.save(dict(vec=vec, clf=clf, mask=True), OUTPUTS / "models" / "ata_sdr.joblib")
    (OUTPUTS / "nlp").mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "nlp" / "ata.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    from airpower.nlp import ata as _m
    sys.exit(_m.main())
