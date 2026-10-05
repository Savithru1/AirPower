"""Weibull life analysis with right censoring and left truncation (maximum likelihood).

    log L = sum_failures log f(t) + sum_censored log S(t) - sum_all log S(entry)

f, S: Weibull density and survival with shape beta and scale eta. Left truncation matters here:
a unit already fitted when the data starts is only observed because it survived to its entry
age, and ignoring that biases the scale upward. Interval estimates come from the observed
information (inverse Hessian of the log-parameters).
"""
import math

import numpy as np
from scipy.optimize import minimize


def _nll(params, t, event, entry):
    lb, le = params
    b, eta = math.exp(lb), math.exp(le)
    z = (t / eta) ** b
    ll = np.sum(event * (np.log(b) - np.log(eta) + (b - 1) * np.log(np.maximum(t, 1e-9) / eta))) - np.sum(z)
    ll += np.sum((entry / eta) ** b)
    return -ll


def fit(t, event, entry=None):
    """t: exit ages, event: 1 failure / 0 censored, entry: ages at entry (0 if from new).
    Returns dict(beta, eta, beta_ci, eta_ci, n, failures) or None if not identifiable."""
    t = np.asarray(t, float)
    event = np.asarray(event, float)
    entry = np.zeros_like(t) if entry is None else np.asarray(entry, float)
    ok = np.isfinite(t) & np.isfinite(entry) & (t > entry)
    t, event, entry = t[ok], event[ok], entry[ok]
    if event.sum() < 3:
        return None
    x0 = [0.0, math.log(max(np.mean(t) * 1.2, 1.0))]
    res = minimize(_nll, x0, args=(t, event, entry), method="Nelder-Mead", options=dict(xatol=1e-6, fatol=1e-8, maxiter=4000))
    lb, le = res.x
    # numerical Hessian for the log-parameters
    h = 1e-4
    H = np.zeros((2, 2))
    for i in range(2):
        for j in range(2):
            e_i, e_j = np.eye(2)[i] * h, np.eye(2)[j] * h
            H[i, j] = (_nll(res.x + e_i + e_j, t, event, entry) - _nll(res.x + e_i - e_j, t, event, entry)
                       - _nll(res.x - e_i + e_j, t, event, entry) + _nll(res.x - e_i - e_j, t, event, entry)) / (4 * h * h)
    try:
        se = np.sqrt(np.diag(np.linalg.inv(H)))
    except np.linalg.LinAlgError:
        se = np.array([np.nan, np.nan])
    b, eta = math.exp(lb), math.exp(le)
    return dict(beta=b, eta=eta, beta_ci=[b * math.exp(-1.96 * se[0]), b * math.exp(1.96 * se[0])],
                eta_ci=[eta * math.exp(-1.96 * se[1]), eta * math.exp(1.96 * se[1])],
                n=int(len(t)), failures=int(event.sum()), converged=bool(res.success))


def mean_life(beta, eta):
    return eta * math.gamma(1 + 1 / beta)


def survival(t, beta, eta):
    return np.exp(-(np.asarray(t, float) / eta) ** beta)
