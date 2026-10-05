"""Phase 3 health models: feature maths, conformal intervals, and the real-data models where the
datasets are present.

    python -m pytest -q tests/test_health_models.py
"""
import numpy as np
import pandas as pd
import pytest

from airpower.health import engine_rul as ER
from airpower.health import fleet_risk as FR
from data_prep import sources


def _toy(units=6, life=120, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for u in range(1, units + 1):
        L = life + 10 * u
        for c in range(1, L + 1):
            wear = (c / L) ** 2
            r = dict(unit=u, cycle=c, setting1=0.0, setting2=0.0, setting3=100.0)
            for i in range(1, 22):
                r[f"s{i}"] = 1.0 if i in (1, 5) else 100 + i + (5 * wear if i in (2, 3, 4) else 0) + rng.normal(0, 0.1)
            r["rul"] = L - c
            rows.append(r)
    return pd.DataFrame(rows)


def test_window_slope_matches_least_squares():
    d = _toy(units=1)
    m = ER.EngineRUL("FD001").fit_norm(d)
    F = m.features(m.normalise(d))
    x = m.normalise(d)["s2"].to_numpy()
    k = 80
    w = x[k - ER.WINDOW + 1: k + 1]
    expect = np.polyfit(np.arange(len(w)), w, 1)[0]
    assert F["s2_slope"].iloc[k] == pytest.approx(expect, rel=1e-6)
    assert "s1_mean" not in F                                   # constant sensor dropped


def test_engine_model_learns_and_intervals_cover():
    d = _toy(units=14)
    m = ER.EngineRUL("FD001").fit(d, calib_frac=0.3)
    test = _toy(units=4, seed=9)
    p = m.predict_frame(test).merge(test[["unit", "cycle", "rul"]].rename(columns={"rul": "true"}), on=["unit", "cycle"])
    true = np.minimum(p["true"], ER.CAP)
    assert np.sqrt(np.mean((p["rul"] - true) ** 2)) < 30
    cover = ((p["lo"] <= true) & (true <= p["hi"])).mean()
    assert cover > 0.75


def test_nasa_score_penalises_late_more_than_early():
    assert ER.nasa_score([10]) > ER.nasa_score([-10]) > 0


def test_fast_theil_sen_matches_scipy():
    from scipy.stats import theilslopes
    rng = np.random.default_rng(4)
    x = np.cumsum(rng.random(30))
    y = 1 - 0.002 * x + rng.normal(0, 0.01, 30)
    y[5] += 0.3                                                   # an outlier does not move it
    sl, ic = FR.theil_sen(x, y)
    ref = theilslopes(y, x)
    assert sl == pytest.approx(ref[0]) and ic == pytest.approx(ref[1])


def test_status_rules():
    assert FR.status_of(0.6) == "alert" and FR.status_of(0.3) == "watch" and FR.status_of(0.05) == "ok"
    assert FR.status_of(0.0, alarm=True) == "alert"
    assert FR.pos_key("ENG_1") == FR.pos_key("ENG-1") == FR.pos_key("ENG#1") == "ENG-1"


@pytest.mark.data
def test_real_engine_model_on_fd001():
    if not sources.available("cmapss"):
        pytest.skip("C-MAPSS not available")
    r = ER.evaluate("FD001")
    assert r["rmse"] < 18 and 0.8 < r["coverage_last_cycle"] <= 1.0
