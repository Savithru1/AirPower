"""Phase 4: Weibull with censoring and truncation, rogue test, repeat-defect chains, ATA reference mask.

    python -m pytest -q tests/test_reliability.py
"""
import numpy as np
import pandas as pd
import pytest

from airpower.nlp.ata import REF, prep
from airpower.reliability import analyse as A
from airpower.reliability import weibull as W


def _lives(n, beta, eta, seed=0):
    rng = np.random.default_rng(seed)
    return eta * (-np.log(rng.random(n))) ** (1 / beta)


def test_weibull_recovers_parameters_with_censoring():
    t = _lives(800, 2.0, 1000.0)
    c = np.random.default_rng(1).uniform(200, 2500, len(t))             # random right censoring
    obs, ev = np.minimum(t, c), (t <= c).astype(int)
    w = W.fit(obs, ev)
    assert w["beta"] == pytest.approx(2.0, rel=0.12) and w["eta"] == pytest.approx(1000.0, rel=0.08)
    assert w["beta_ci"][0] < 2.0 < w["beta_ci"][1]


def test_left_truncation_is_needed_and_handled():
    """Units observed only after surviving to an entry age: ignoring the entry biases the fit."""
    t = _lives(4000, 2.5, 1000.0, seed=3)
    entry = np.random.default_rng(4).uniform(0, 900, len(t))
    keep = t > entry
    t, entry = t[keep], entry[keep]
    ev = np.ones_like(t)
    ok = W.fit(t, ev, entry)
    naive = W.fit(t, ev)
    assert ok["eta"] == pytest.approx(1000.0, rel=0.08)
    assert abs(naive["eta"] - 1000.0) > abs(ok["eta"] - 1000.0)


def test_too_few_failures_returns_none():
    assert W.fit([100, 200, 300], [1, 0, 0]) is None


def test_rogue_likelihood_ratio_flags_short_lives():
    parts = pd.DataFrame(dict(pn=["P"], weibull_beta=[2.0], weibull_eta_h=[1000.0]))
    rows = []
    for k in range(60):                                                  # normal units: one life ~ 900 h
        rows.append(dict(pn="P", sn=f"N{k}", tail="J1", end=pd.Timestamp("2026-06-01"), outcome="failure",
                         exposure_fh=900.0, entry_fh=0.0, exit_fh=900.0))
    for _ in range(3):                                                   # one unit failing every ~250 h
        rows.append(dict(pn="P", sn="ROGUE", tail="J2", end=pd.Timestamp("2026-06-01"), outcome="failure",
                         exposure_fh=250.0, entry_fh=0.0, exit_fh=250.0))
    R = A.rogues(pd.DataFrame(rows), parts).set_index("sn")
    assert bool(R.loc["ROGUE", "rogue"]) and not R.drop(index="ROGUE")["rogue"].any()


def test_reference_mask_hides_chapter_citations():
    s = pd.Series(["REPLACED TIRE PER AMM 32-45-05-4 AND SERVICED PER AMM 12-15-03", "SKIN CRACK REPAIRED IAW SRM 53-00-01"])
    m = prep(s, mask=True)
    assert "32" not in m.iloc[0] and "53" not in m.iloc[1] and "tire" in m.iloc[0]
    assert s.str.contains(REF).all()
