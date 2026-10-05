"""Remaining-useful-life prognostics with NASA ProgPy.

Copied from AeroTwin (sih26054_v2/prognostics/rul.py). Unchanged except `run_rul`'s
`min_samples` argument, so per-sortie health indices (a few samples per window, not 1 Hz
data) can be filtered too. Time is in whatever unit the caller uses (AeroTwin: hours).

Degradation model (ProgPy PrognosticsModel), for a twin health parameter h that
starts near 1 and fails at a threshold h_fail:
    dh/dt = -r            r  : current degradation rate   (1/h)
    dr/dt = gamma * r     gamma: acceleration (1/h), unknown, estimated
State estimation: SIR particle filter (log-weights, systematic resampling,
roughening) over the ProgPy model state, fed every `every_h` with the twin's
estimate of h (e.g. the UKF oil-pressure factor theta_p) and its uncertainty.
(ProgPy's own ParticleFilter degenerated on this accelerating model: weights
underflow when degradation outruns the prior, so a log-domain PF is used.)
Prediction: ProgPy MonteCarlo -> distribution of the time the threshold is met
-> RUL median and 95 % interval.  Evaluated with the alpha-lambda metric
against the ground-truth end-of-life.
"""
import numpy as np
from progpy import PrognosticsModel
from progpy.predictors import MonteCarlo
from progpy.uncertain_data import UnweightedSamples


class AcceleratingDegradation(PrognosticsModel):
    inputs = []
    states = ["h", "r", "gamma"]
    outputs = ["h_meas"]
    events = ["EOL"]
    default_parameters = {
        "x0": {"h": 1.0, "r": 0.001, "gamma": 0.1},
        "process_noise": {"h": 1e-5, "r": 1e-5, "gamma": 2e-3},
        "measurement_noise": {"h_meas": 0.02},
        "h_fail": 0.6,
    }

    def next_state(self, x, u, dt):                 # dt in hours
        r = np.abs(x["r"])
        g = np.clip(x["gamma"], -0.5, 1.5)
        x["h"] = x["h"] - r * dt
        x["r"] = r * np.exp(g * dt)
        x["gamma"] = g
        return x

    def output(self, x):
        return self.OutputContainer({"h_meas": x["h"]})

    def event_state(self, x):
        hf = self.parameters["h_fail"]
        return {"EOL": np.clip((x["h"] - hf) / (1.0 - hf), 0.0, 1.0)}

    def threshold_met(self, x):
        return {"EOL": x["h"] <= self.parameters["h_fail"]}


class SIRParticleFilter:
    """Sampling-importance-resampling particle filter over the ProgPy model's state,
    with log-domain weights (no underflow when the degradation outruns the prior),
    systematic resampling at ESS < N/2 and roughening (regularised PF)."""

    def __init__(self, model, x0, n=1000, t0=0.0, rough=(0.004, 0.003, 0.05), seed=0):
        self.m, self.n, self.t = model, n, t0
        self.rng = np.random.default_rng(seed)
        self.x = {k: self.rng.normal(mu, sd, n) for k, (mu, sd) in x0.items()}
        self.x["r"] = np.abs(self.x["r"])
        self.w = np.full(n, 1.0 / n)
        self.rough = dict(zip(["h", "r", "gamma"], rough))

    def step(self, t, z, z_sd):
        dt = t - self.t
        self.t = t
        self.x = self.m.next_state(dict(self.x), {}, dt)
        for k, s in self.rough.items():
            self.x[k] = self.x[k] + self.rng.normal(0, s * np.sqrt(dt), self.n)
        self.x["r"] = np.abs(self.x["r"])
        logw = np.log(self.w + 1e-300) - 0.5 * ((z - self.x["h"]) / z_sd) ** 2
        logw -= logw.max()
        w = np.exp(logw)
        self.w = w / w.sum()
        if 1.0 / np.sum(self.w ** 2) < self.n / 2:
            pos = (self.rng.random() + np.arange(self.n)) / self.n
            idx = np.minimum(np.searchsorted(np.cumsum(self.w), pos), self.n - 1)
            self.x = {k: v[idx] for k, v in self.x.items()}
            self.w = np.full(self.n, 1.0 / self.n)

    def mean(self):
        return {k: float(np.sum(self.w * v)) for k, v in self.x.items()}

    def samples(self, k):
        idx = self.rng.choice(self.n, size=k, p=self.w)
        return [{kk: float(v[i]) for kk, v in self.x.items()} for i in idx]


def run_rul(t_hours, h, h_sd, h_fail=0.6, start_h=None, every_h=0.25, n_particles=1000, horizon_h=30.0,
            n_pred=150, seed=0, min_samples=10):
    """t_hours, h, h_sd: twin health-parameter time series.  Returns one dict per update
    with the particle-filter state and the ProgPy Monte-Carlo RUL distribution.
    An update needs at least `min_samples` measurements in its window."""
    m = AcceleratingDegradation(h_fail=h_fail)
    ok = np.isfinite(h)
    t_hours, h, h_sd = t_hours[ok], h[ok], h_sd[ok]
    t_start = start_h if start_h is not None else t_hours[0]
    h0 = float(np.median(h[(t_hours >= t_start) & (t_hours < t_start + 0.5)])) if np.any(t_hours >= t_start) else 1.0
    pf = SIRParticleFilter(m, {"h": (h0, 0.01), "r": (0.0, 0.004), "gamma": (0.0, 0.25)}, n=n_particles, t0=t_start, seed=seed)
    mc = MonteCarlo(m)
    out = []
    for tg in np.arange(t_start + every_h, t_hours[-1] + 1e-9, every_h):
        w = (t_hours > tg - every_h) & (t_hours <= tg)
        if w.sum() < min_samples:
            continue
        z = float(np.median(h[w]))
        zsd = float(max(np.std(h[w]) / np.sqrt(max(w.sum() / 60.0, 1.0)), np.median(h_sd[w]) * 0.3, 0.004))
        pf.step(float(tg), z, zsd)
        mean = pf.mean()
        rec = dict(t=float(tg), h=z, h_est=mean["h"], rate=mean["r"], gamma=mean["gamma"])
        res = mc.predict(UnweightedSamples(pf.samples(n_pred)), dt=0.05, horizon=horizon_h, save_freq=horizon_h)
        toe = np.array([s["EOL"] if s["EOL"] is not None else np.inf for s in res.time_of_event], dtype=float)
        rul = toe                                   # MonteCarlo times are relative to the prediction start
        fin = np.isfinite(rul)
        rec["p_fail_in_horizon"] = float(fin.mean())
        if fin.mean() >= 0.5:
            q = np.percentile(np.where(fin, rul, 2 * horizon_h), [2.5, 50, 97.5])
            rec.update(rul_lo=float(q[0]), rul_med=float(q[1]), rul_hi=float(min(q[2], 2 * horizon_h)))
        else:
            rec.update(rul_lo=None, rul_med=None, rul_hi=None)
        out.append(rec)
    return out


def alpha_lambda(preds, t_eol, alpha=0.2):
    """Fraction of predictions (after the first one with a finite RUL) whose median lies
    within +-alpha x true RUL, and the 95 % interval coverage of the true RUL."""
    ok = [p for p in preds if p["rul_med"] is not None and p["t"] < t_eol]
    if not ok:
        return dict(n=0)
    hits = [abs(p["rul_med"] - (t_eol - p["t"])) <= alpha * (t_eol - p["t"]) for p in ok]
    cover = [p["rul_lo"] <= (t_eol - p["t"]) <= p["rul_hi"] for p in ok]
    return dict(n=len(ok), alpha=alpha, alpha_lambda_accuracy=float(np.mean(hits)), ci95_coverage=float(np.mean(cover)),
                first_prediction_h=ok[0]["t"], t_eol_true_h=t_eol)
