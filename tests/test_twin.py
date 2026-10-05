"""Phase 6-7: the predictive policy in the simulator, kits in the stores, the twin started from the
records, the twin views and their API routes.

    python -m pytest -q tests/test_twin.py
"""
import shutil

import numpy as np
import pytest

from airpower.fleetsim import simulate, truth

H = {"X-Requested-With": "airpower"}          # the API requires it on every state-changing call


def _kpi(sim):
    return truth.kpis(sim, truth.tables(sim))["by_unit"]["FLEET"]


def test_p2_runs_and_reports():
    """P2 with rule-based timing on parametric signals (no datasets needed): it runs, draws kits,
    and every kit it draws is used, handed over or still pending at the end."""
    sim = simulate(seed=5, policy="P2", policy_args=dict(planner=None, rates_db="sqlite:///missing.sqlite"),
                   days=120, real_signals=False)
    s = sim.policy.summary()
    assert s["kits_drawn"] > 0
    assert s["kits_used"] + s["kits_handed_over"] <= s["kits_drawn"]
    assert 0.0 < _kpi(sim)["availability"] <= 1.0
    assert s["positioned"] >= 0


def test_p2_with_planner_solves():
    sim = simulate(seed=6, policy="P2", policy_args=dict(time_limit=2.0, rates_db="sqlite:///missing.sqlite"),
                   days=35, real_signals=False)
    s = sim.policy.summary()
    assert s["plans_solved"] >= 4 and s["plans_failed"] == 0


def test_default_streams_unchanged_and_crn_deterministic():
    """The shared random stream is the default (Phase 1-5 runs reproduce); per-aircraft streams are
    deterministic too."""
    a = _kpi(simulate(seed=3, days=60, real_signals=False))
    b = _kpi(simulate(seed=3, days=60, real_signals=False))
    assert a == b
    c = _kpi(simulate(seed=3, days=60, real_signals=False, overrides={"crn_streams": True}))
    d = _kpi(simulate(seed=3, days=60, real_signals=False, overrides={"crn_streams": True}))
    assert c == d


def test_kits_wait_behind_grounded_aircraft():
    """A returning unit goes to an aircraft already waiting before a kit drawn ahead of a removal."""
    sim = simulate(seed=2, days=1, real_signals=False)
    st = sim.stores
    pn = next(p for p in sim.parts if not sim.parts[p][0].consumable)
    for loc in st.locs:
        st.stock[loc][pn].clear()
    ev_kit, _ = st.request("BN", pn, "J1001", "KIT", kit=True)
    ev_real, _ = st.request("BN", pn, "J1002", "WO1")
    st.arrive(sim.new_serial(sim.parts[pn][0]), "BN")
    sim.env.run(until=sim.env.now + 48)
    assert ev_real.triggered and not ev_kit.triggered


def test_twin_state_conditions_on_age():
    """Started from recorded ages, every life is drawn beyond the recorded age."""
    sim0 = simulate(seed=4, days=1, real_signals=False)
    ac = sim0.aircraft[0]
    state = dict(aircraft={ac.tail: dict(fh=ac.fh + 100, inspections={}, slots={sl: dict(age_fh=500.0) for sl in ac.slots})})
    from airpower.fleetsim.config import load
    from airpower.fleetsim.engine import FleetSim
    from airpower.fleetsim.policies import Reactive
    f = FleetSim(load(), seed=4, policy=Reactive(), days=1, real_signals=False, state=state)
    a = next(x for x in f.aircraft if x.tail == ac.tail)
    for sl, s in a.slots.items():
        if s is not None and np.isfinite(s.ttf):
            assert s.ttf > s.clock() - 1e-9, sl
            assert abs(s.age_fh - 500.0) < 1e-6


# ---------------------------------------------------------------------- on the dashboard store
@pytest.fixture(scope="module")
def store(tmp_path_factory):
    from airpower.config import OUTPUTS
    src = OUTPUTS / "airpower.sqlite"
    if not src.exists():
        pytest.skip("store not loaded")
    dst = tmp_path_factory.mktemp("store") / "airpower.sqlite"
    shutil.copy(src, dst)
    return f"sqlite:///{dst.as_posix()}"


@pytest.mark.data
def test_views_on_store(store):
    from server.app import Store
    from airpower.twin import views as V
    q = Store(store).q
    hm = V.health_map(q)
    assert len(hm["rows"]) == 48 and "Engines" in hm["columns"]
    tail = hm["rows"][-1]["tail"]
    t = V.tail_twin(q, tail)
    assert len(t["configuration"]) > 10 and len(t["inspections"]) >= 3
    assert all(a["priority"] in (1, 2, 3) and a["why"] for a in t["actions"])


@pytest.mark.data
def test_whatif_from_store(store):
    from airpower.twin import whatif as W
    st = W.from_store(store)
    assert len(st["aircraft"]) == 48 and sum(len(a["slots"]) for a in st["aircraft"].values()) > 1000
    r = W.forecast({"flying": 1.3}, store, days=10, reps=2, procs=2, use_cache=False, state=st)
    assert len(r["baseline"]["daily"]) == 10 and r["levers"]["flying"] == 1.3


@pytest.mark.data
def test_api_routes(store):
    from fastapi.testclient import TestClient
    from server.app import create_app
    c = TestClient(create_app(store, auth=False, warm=False))
    hm = c.get("/api/healthmap").json()
    tail = hm["rows"][0]["tail"]
    assert c.get(f"/api/aircraft/{tail}/twin").status_code == 200
    assert c.get("/api/aircraft/X9999/twin").status_code == 404
    r = c.get(f"/report/aircraft/{tail}")
    assert r.status_code == 200 and tail in r.text and "<table" in r.text
    assert c.get("/report/fleet").status_code == 200
    assert c.post("/api/plan/decision", json={"tail": tail, "job": "x", "decision": "maybe"}, headers=H).status_code == 422
    assert c.post("/api/plan/decision", json={"tail": tail, "job": "x", "decision": "override"}, headers=H).status_code == 422
    ok = c.post("/api/plan/decision", json={"tail": tail, "job": "50 h servicing", "decision": "accept"}, headers=H)
    assert ok.status_code == 200 and ok.json()["user"] == "dev"
    assert c.post("/api/whatif", json={"levers": {"warp": 9}}, headers=H).status_code == 422


@pytest.mark.data
def test_plan_honours_decisions(store):
    """A job moved by an engineering officer keeps its new day in the next plan."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from airpower.plan.planner import build
    c = TestClient(create_app(store, auth=False, warm=False))
    jobs = c.get("/api/plan?which=optimised").json()["jobs"]
    j = next(x for x in jobs if x["kind"] == "inspection" and x["start_day"] is not None and 3 <= x["start_day"] < 40)
    day = j["start_day"] - 2
    r = c.post("/api/plan/decision", json={"tail": j["tail"], "job": j["job"], "decision": "override", "start_day": day}, headers=H)
    assert r.status_code == 200 and r.json()["start_date"]
    s = build(store, horizon=60, time_limit=30, store=True, write_file=False)
    assert any(f["tail"] == j["tail"] and f["job"] == j["job"] and f["day"] == day for f in s["fixed_by_decision"])
    new = c.get("/api/plan?which=optimised").json()["jobs"]
    assert any(x["tail"] == j["tail"] and x["job"] == j["job"] and x["start_day"] == day for x in new)
