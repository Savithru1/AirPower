"""Phase 5: METRIC maths, life-limit parsing, and the planner on the integrated store.

    python -m pytest -q tests/test_spares_plan.py
"""
import math

import numpy as np
import pytest
from scipy.stats import poisson

from airpower.spares.forecast import parse_limit
from airpower.spares.metric import ebo


def test_ebo_matches_definition():
    for mean in (0.3, 2.0, 7.5):
        for s in (0, 1, 3, 10):
            x = np.arange(0, 200)
            ref = float(np.sum(np.maximum(x - s, 0) * poisson.pmf(x, mean)))
            assert ebo(mean, s) == pytest.approx(ref, abs=1e-9)
    assert ebo(2.0, 0) == pytest.approx(2.0)                      # no stock: every pipeline unit is a backorder
    assert ebo(0.0, 0) == 0.0


def test_ebo_is_convex_decreasing():
    e = [ebo(3.0, s) for s in range(12)]
    d = np.diff(e)
    assert np.all(d <= 0) and np.all(np.diff(d) >= -1e-12)


def test_greedy_beats_uniform_allocation():
    """Marginal allocation on a toy fleet: same money, more availability than a flat rule."""
    from airpower.spares.metric import Metric
    import pandas as pd

    class Toy(Metric):
        def __init__(self):
            self.N = pd.Series({"T": 20})
            self.Z = pd.DataFrame({"T": [1.0, 1.0, 2.0]}, index=["A", "B", "C"])
            self.rate = pd.DataFrame({"T": [0.05, 0.01, 0.1]}, index=["A", "B", "C"])
            self.pipe = pd.DataFrame({"pipeline_days": [60.0, 120.0, 30.0]}, index=["A", "B", "C"])
            self.cost = pd.Series({"A": 50.0, "B": 300.0, "C": 5.0})
            self.skus = ["A", "B", "C"]
            self.mean = {pn: float(self.rate.loc[pn].sum() * self.pipe.loc[pn, "pipeline_days"]) for pn in self.skus}

    m = Toy()
    flat = {"A": 3, "B": 2, "C": 3}
    budget = sum(flat[k] * m.cost[k] for k in flat)
    best = m.at_budget(m.curve(max_cost=budget), budget)
    assert best["cost_lakh"] <= budget + 1e-9
    assert m.availability(best["stock"])[0] > m.availability(flat)[0]


def test_parse_limit():
    assert parse_limit("fh=1500;days=730") == {"fh": 1500.0, "days": 730.0}
    assert parse_limit(None) == {} and parse_limit("") == {}


@pytest.mark.data
def test_planner_on_store():
    """Needs the dashboard store (python -m airpower.ingest.load ...); solves 48 aircraft x 90 days."""
    from airpower.config import OUTPUTS
    if not (OUTPUTS / "airpower.sqlite").exists():
        pytest.skip("store not loaded")
    from airpower.plan.planner import build
    s = build(horizon=60, time_limit=30, store=False, write_file=False)
    o, b = s["optimised_plan"], s["due_date_plan"]
    assert s["solver"]["status"] in ("OPTIMAL", "FEASIBLE")
    assert o["shortfall_aircraft_days"] <= b["shortfall_aircraft_days"]
    assert o["down_aircraft_days"] <= b["down_aircraft_days"]
