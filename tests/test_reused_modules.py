"""Modules copied or generalised from AeroTwin: detection, diagnosis, RUL particle filter,
vibration features, signed ingest envelope.

    python -m pytest -q tests/test_reused_modules.py
"""
import numpy as np
import pandas as pd
import pytest

from airpower.health import detect as D
from airpower.health import vibration as V
from airpower.health.diagnose import Diagnoser
from airpower.ingest.secure import SignedJson, default_key

CH = ["a", "b"]


def _healthy(n=600, seed=0):
    rng = np.random.default_rng(seed)
    z = pd.DataFrame({"t": np.arange(n, dtype=float), "a": rng.normal(0, 1, n), "b": rng.normal(0, 1, n)})
    return z, D.smoothed(z, CH)


# ------------------------------------------------------------------ detection
def test_thresholds_have_floor_and_scale_with_margin():
    _, s = _healthy()
    mx = [D.healthy_maxima(s, CH)]
    th1 = D.thresholds_from_maxima(mx, CH, floor=0.0, margin=1.0)
    th2 = D.thresholds_from_maxima(mx, CH, floor=0.0, margin=2.0)
    assert th2["a"] == pytest.approx(2 * th1["a"])
    assert D.thresholds_from_maxima(mx, CH, floor=5.0)["a"] == 5.0          # EWMA of N(0,1) stays well below 5


def test_alarm_needs_persistence_and_reports_channel():
    z, _ = _healthy()
    z.loc[300:, "b"] += 12.0                                               # step fault on channel b
    s = D.smoothed(z, CH)
    det = D.detect(z, s, {"a": 5.0, "b": 5.0}, CH)
    t, why = D.first_alarm(det)
    assert t is not None and 300 + D.PERSIST - 1 <= t < 300 + 60 and why == "b"
    assert not det.loc[:299, "alarm"].any()
    # persistence rule on its own (unsmoothed input): an excursion shorter than PERSIST never alarms
    raw = pd.DataFrame({"a": np.zeros(200), "b": np.zeros(200)})
    raw.loc[50:50 + D.PERSIST - 2, "b"] = 50.0
    assert not D.alarm_mask(raw, raw, {"a": 5.0, "b": 5.0}, CH).any()
    raw.loc[50:50 + D.PERSIST - 1, "b"] = 50.0
    assert D.alarm_mask(raw, raw, {"a": 5.0, "b": 5.0}, CH).sum() == 1


def test_inactive_rows_are_ignored():
    z, _ = _healthy()
    z["active"] = True
    z.loc[100:400, "active"] = False
    z.loc[100:400, "a"] = 100.0
    det = D.detect(z, D.smoothed(z, CH), {"a": 5.0, "b": 5.0}, CH)
    assert not det["alarm"].any() and det.loc[100:400, "HI"].isna().all()


def test_select_margin_prefers_smallest_margin_meeting_target():
    groups = {g: [_healthy(seed=10 * g + k) for k in range(3)] for g in range(4)}
    hours = {g: 3.0 for g in groups}
    out = D.select_margin(groups, CH, hours, margins=(0.5, 1.0, 1.5, 3.0), target_fa_per_100h=0.0, floor=0.0)
    fa = {r["margin"]: r["fa_per_100h"] for r in out["rows"]}
    assert fa[0.5] > 0 and fa[3.0] == 0                                     # tight margins alarm on new aircraft
    assert out["chosen_margin"] == min(m for m, v in fa.items() if v == 0)


# ------------------------------------------------------------------ diagnosis
def _classes(n=300, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(0, 1, (3 * n, 3)), columns=["x1", "x2", "x3"])
    y = np.array(["nominal"] * n + ["wear"] * n + ["crack"] * n, dtype=object)
    X.loc[n:2 * n - 1, "x1"] += 4.0
    X.loc[2 * n:, "x2"] += 4.0
    return X, y


def test_diagnoser_classifies_explains_and_flags_novelty():
    X, y = _classes()
    dg = Diagnoser(readable={"x1": "wear indicator"}).fit(X, y)
    probe = pd.DataFrame({"x1": [4.0], "x2": [0.0], "x3": [0.0]})
    cls, p, basis = dg.decide(dg.predict_proba(probe).iloc[0])
    assert (cls, basis) == ("wear", "classifier") and p > 0.5
    top = dg.explain(probe, "wear", top=1)[0]
    assert top["feature"] == "x1" and top["meaning"] == "wear indicator"
    far = pd.DataFrame({"x1": [0.0], "x2": [0.0], "x3": [15.0]})
    near = pd.DataFrame({"x1": [0.0], "x2": [0.0], "x3": [0.0]})
    assert dg.novelty(far)[0] > dg.novelty(near)[0]


def test_diagnoser_says_unknown_when_not_confident():
    dg = Diagnoser()
    assert dg.decide(pd.Series({"nominal": 0.85, "wear": 0.1, "crack": 0.05}))[0] == "unknown"


# ------------------------------------------------------------------ RUL particle filter
def test_rul_pf_tracks_accelerating_degradation():
    pytest.importorskip("progpy")
    from airpower.health.rul_pf import alpha_lambda, run_rul
    rng = np.random.default_rng(3)
    t = np.arange(0, 40.0, 1 / 60)                                         # hours, one sample a minute
    h_true = 1.0 - 0.004 * t - 0.0002 * t ** 2
    h = h_true + rng.normal(0, 0.01, len(t))
    t_eol = float(t[np.argmax(h_true <= 0.6)])
    preds = run_rul(t, h, np.full(len(t), 0.01), h_fail=0.6, every_h=2.0, horizon_h=60.0, n_particles=400,
                    n_pred=80, seed=1)
    assert preds and all(p["h_est"] <= 1.05 for p in preds)
    late = [p for p in preds if p["rul_med"] is not None and p["t"] > 0.5 * t_eol]
    assert late, "filter never produced a finite RUL"
    err = [abs(p["rul_med"] - (t_eol - p["t"])) for p in late]
    assert np.median(err) < 0.35 * t_eol
    assert alpha_lambda(preds, t_eol)["n"] > 0


# ------------------------------------------------------------------ vibration features
def _tone(f, amp, n=4096, seed=0, noise=0.01):
    t = np.arange(n) / V.FS
    rng = np.random.default_rng(seed)
    return amp * np.sin(2 * np.pi * f * t) + rng.normal(0, noise, n)


def test_order_peak_recovers_tone_amplitude():
    rpm = 6000.0                                                           # 1E = 100 Hz
    x = np.stack([_tone(2 * rpm / 60.0, 0.3)])
    f = V.features(x, np.array([rpm]), np.array([rpm / 2.54]))
    assert f["Vib_2E"][0] == pytest.approx(0.3, rel=0.15)
    assert f["Vib_05E"][0] < 0.05


def test_impacts_raise_kurtosis_and_envelope_modulation():
    rng = np.random.default_rng(1)
    n = 4096
    smooth = rng.normal(0, 0.1, n)
    impacts = smooth.copy()
    ring = np.sin(2 * np.pi * 2500 * np.arange(64) / V.FS) * np.exp(-np.arange(64) / 10)
    for k in range(0, n - 64, 400):                                        # periodic bearing-like impacts
        impacts[k:k + 64] += 2.0 * ring
    x = np.stack([smooth, impacts])
    f = V.features(x, np.array([3000.0, 3000.0]), np.array([1200.0, 1200.0]))
    assert f["Vib_Kurtosis"][1] > 2 * f["Vib_Kurtosis"][0]
    assert f["Vib_EnvMod"][1] > f["Vib_EnvMod"][0]


# ------------------------------------------------------------------ signed envelope
def test_signed_json_roundtrip_and_rejections():
    tx, rx = SignedJson(default_key()), SignedJson(default_key())
    msg = tx.seal({"tail": "J1001", "v": float("nan")}, now=1000.0)
    assert rx.open(msg, now=1001.0) == {"tail": "J1001", "v": None}
    with pytest.raises(ValueError, match="replayed"):
        rx.open(msg, now=1001.0)
    with pytest.raises(ValueError, match="stale"):
        rx.open(tx.seal({"x": 1}, now=1000.0), now=1100.0)
    with pytest.raises(ValueError, match="stale"):                         # forged future timestamp
        rx.open(tx.seal({"x": 1}, now=5000.0), now=1000.0)
    with pytest.raises(ValueError, match="bad signature"):
        SignedJson(default_key("other")).open(tx.seal({"x": 1}, now=1000.0), now=1000.0)
