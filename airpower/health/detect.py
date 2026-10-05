"""Anomaly detection on normalised health-indicator residuals.

Generalised from AeroTwin (sih26054_v2/twin/detect.py and the leave-one-aircraft-out margin
selection in twin/pipeline.py `tail_cv`). The rules are the same; the channel list is now an
argument instead of AeroTwin's fixed engine channels, so any HUMS indicator set can use it.

* channel alarm: |EWMA(z)| above the channel threshold for >= `persist` consecutive samples;
* thresholds learned on healthy data: margin x the largest sustained healthy excursion,
  with a floor (5 sigma by default);
* the margin is chosen by leave-one-group-out cross-validation (a group is an aircraft),
  because a threshold that only just covers the training aircraft does not cover a new one;
* health index HI = max over channels of |EWMA(z)| / threshold (1.0 = alarm level).

A residual table `z` has one row per sample, a column per channel, and optionally an
`active` column (False rows are ignored, e.g. engine not running).
"""
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

PERSIST = 20
EWMA_ALPHA = 0.1


def ewma(x, alpha=EWMA_ALPHA):
    return pd.Series(x).ewm(alpha=alpha, ignore_na=True).mean().to_numpy()


def _active(z):
    return z["active"].to_numpy(dtype=bool) if "active" in z else np.ones(len(z), dtype=bool)


def smoothed(z, channels, alpha=EWMA_ALPHA):
    """EWMA of every channel, restarted at each active segment."""
    act = _active(z)
    seg = np.cumsum(np.r_[1, np.diff(act.astype(int)) != 0])
    out = {}
    for c in channels:
        x = z[c].to_numpy(dtype=float).copy()
        x[~act] = np.nan
        out[c] = pd.Series(x).groupby(seg).transform(
            lambda v: v.ewm(alpha=alpha, ignore_na=True).mean()).to_numpy()
    return pd.DataFrame(out, index=z.index)


def sustained_max(x, n):
    """Largest value v such that |x| >= v for n consecutive samples."""
    x = np.nan_to_num(np.abs(np.asarray(x, dtype=float)), nan=0.0)
    if len(x) < n:
        return 0.0
    return float(sliding_window_view(x, n).min(axis=1).max())


def persist(flag, n):
    """True where flag has been true for the last n samples."""
    f = pd.Series(np.asarray(flag, dtype=float)).rolling(n, min_periods=n).sum().to_numpy()
    return f >= n


def healthy_maxima(s, channels, n=PERSIST):
    """Largest sustained healthy excursion of every channel in one smoothed series."""
    return {c: sustained_max(s[c].to_numpy(), n) for c in channels}


def thresholds_from_maxima(maxima, channels, floor=5.0, margin=1.15):
    """maxima: list of healthy_maxima dicts (one per healthy series)."""
    return {c: float(max(floor, margin * max((m.get(c, 0.0) for m in maxima), default=0.0)))
            for c in channels}


def alarm_mask(z, s, th, channels, n=PERSIST):
    act = _active(z)
    fired = np.zeros(len(z), dtype=bool)
    for c in channels:
        fired |= persist((np.nan_to_num(np.abs(s[c].to_numpy()), nan=0.0) >= th[c]) & act, n)
    return fired


def detect(z, s, th, channels, n=PERSIST, t_col="t"):
    """Per-sample HI, alarm flag and the alarming channels."""
    act = _active(z)
    ratio = np.stack([np.nan_to_num(np.abs(s[c].to_numpy()) / th[c], nan=0.0) for c in channels], axis=1)
    hi = ratio.max(axis=1)
    hi[~act] = np.nan
    fired = np.zeros(len(z), dtype=bool)
    reasons = [[] for _ in range(len(z))]
    for j, c in enumerate(channels):
        f = persist((ratio[:, j] >= 1.0) & act, n)
        fired |= f
        for i in np.flatnonzero(f):
            reasons[i].append(c)
    t = z[t_col].to_numpy() if t_col in z else np.arange(len(z), dtype=float)
    out = pd.DataFrame({"t": t, "active": act, "HI": hi, "alarm": fired})
    out["reasons"] = [",".join(r) for r in reasons]
    return out


def first_alarm(det, t_from=None):
    a = det[det["alarm"] & ((det["t"] >= t_from) if t_from is not None else True)]
    return (float(a["t"].iloc[0]), a["reasons"].iloc[0]) if len(a) else (None, "")


def alarm_episodes(mask):
    """Number of rising edges of a boolean alarm mask."""
    return int(np.sum(np.diff(np.r_[0, np.asarray(mask, dtype=int)]) == 1))


def select_margin(healthy, channels, hours, margins=(1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0),
                  target_fa_per_100h=5.0, floor=5.0, n=PERSIST):
    """Leave-one-group-out choice of the threshold margin.

    healthy: {group: [(z, s), ...]} healthy residual / smoothed tables per group (aircraft);
    hours:   {group: operating hours represented by that group's healthy tables}.
    For each margin, thresholds are fitted on all other groups and false-alarm episodes are
    counted on the held-out group. Returns the smallest margin whose cross-validated rate
    meets the target, with the per-margin table.
    """
    maxima = {g: [healthy_maxima(s, channels, n) for _, s in tables] for g, tables in healthy.items()}
    total_h = max(sum(hours.values()), 1e-9)
    rows = []
    for margin in margins:
        episodes = 0
        for g, tables in healthy.items():
            others = [m for k, ms in maxima.items() if k != g for m in ms]
            th = thresholds_from_maxima(others, channels, floor=floor, margin=margin)
            episodes += sum(alarm_episodes(alarm_mask(z, s, th, channels, n)) for z, s in tables)
        rows.append(dict(margin=margin, fa_episodes=episodes, fa_per_100h=100.0 * episodes / total_h))
    ok = [r for r in rows if r["fa_per_100h"] <= target_fa_per_100h]
    return dict(chosen_margin=(ok[0] if ok else rows[-1])["margin"], target_fa_per_100h=target_fa_per_100h,
                rows=rows)
