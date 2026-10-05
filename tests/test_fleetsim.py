"""FleetSim: configuration, invariants of a run, exports.

    python -m pytest -q tests/test_fleetsim.py
"""
import json
from collections import Counter

import pandas as pd
import pytest

from airpower.fleetsim import config as C
from airpower.fleetsim import exports, simulate, truth
from airpower.fleetsim.engine import AVAILABLE


@pytest.fixture(scope="module")
def sim():
    return simulate(days=120, seed=7, real_signals=False)


def test_config_loads_two_types():
    cfg = C.load()
    assert set(cfg.types) == {"J", "H"}
    j = cfg.types["J"]
    assert j.position("ENG").hums["kind"] == "cmapss" and j.position("CART").policy == "HT"
    assert sum(u["aircraft"] for u in cfg.units) == 48
    shared = {pn for pn, specs in cfg.parts().items() if len({s.type_id for s in specs}) > 1}
    assert {"RC-1220-04", "DU-0640-02", "FD-0310-02", "BT-0028-07"} <= shared


def test_same_seed_same_run():
    a = simulate(days=40, seed=3, real_signals=False)
    b = simulate(days=40, seed=3, real_signals=False)
    assert truth.kpis(a, truth.tables(a)) == truth.kpis(b, truth.tables(b))


def test_no_serial_is_in_two_places(sim):
    """A position is empty only while its aircraft waits for the replacement; a serial is never
    fitted twice, nor fitted and in stock at once."""
    slots = [(a, k, s) for a in sim.aircraft for k, s in a.slots.items()]
    assert len(slots) == sum(p.qty * u["aircraft"] for u in sim.cfg.units for p in sim.cfg.types[u["type"]].positions)
    installed = [s.sid for _, _, s in slots if s is not None]
    assert len(installed) == len(set(installed))
    in_stock = {s.sid for loc in sim.stores.stock.values() for lst in loc.values() for s in lst}
    assert not in_stock & set(installed)
    for a, k, s in slots:
        if s is None:
            assert a.items and k in a.open_slot


def test_state_intervals_cover_the_run(sim):
    st = truth.state_intervals(sim)
    per = st.assign(d=st["t_end"] - st["t_start"]).groupby("tail")["d"].sum()
    assert (abs(per - sim.days * 24) < 1e-6).all()
    k = truth.kpis(sim, truth.tables(sim))["by_unit"]["FLEET"]
    assert 0.3 < k["availability"] < 0.95
    assert abs(k["availability"] + k["nmcs"] + k["nmcm"] + k["scheduled"] - 1) < 1e-9


def test_defects_and_changes_are_consistent(sim):
    tab = truth.tables(sim)
    de, ch = tab["defects"], tab["changes"]
    closed = de.dropna(subset=["t_closed"])
    assert (closed["t_closed"] >= closed["t_raised"]).all()
    assert set(ch["defect"]) <= set(de["id"])
    rep = de[de["installed_sn"].notna()]
    assert len(rep) == len(ch)
    assert (de.loc[de["kind"] == "snag", "slot"].isna()).all()


def test_no_start_up_burst():
    """Equilibrium initial ages: removals in the first month are not far above a later month."""
    s = simulate(days=240, seed=11, real_signals=False)
    ch = pd.DataFrame(s.log["changes"])
    m1 = ((ch["t"] < 30 * 24)).sum()
    m6 = ((ch["t"] >= 180 * 24) & (ch["t"] < 210 * 24)).sum()
    assert m1 < 1.8 * max(m6, 1)


def test_exports_write_four_systems_and_log_injections(sim, tmp_path):
    inj = exports.write(sim, tmp_path / "sources")
    src = tmp_path / "sources"
    for f in ["records/aircraft.csv", "records/defects.csv", "records/component_changes.csv", "records/flying_log.csv",
              "agencies/work_orders.csv", "agencies/agencies.json", "stores/transactions.csv", "stores/stock_items.csv"]:
        assert (src / f).exists(), f
    assert list((src / "hums").glob("*/*.jsonl"))
    assert inj.get("defect_missing_ata", 0) > 0 and inj.get("pn_format_variant", 0) > 0
    xw = pd.read_parquet(tmp_path / "truth" / "crosswalk.parquet")
    assert set(xw.loc[xw["entity"] == "defect", "truth_id"]) == {d["id"] for d in sim.log["defects"]}
    lines = (tmp_path / "truth" / "injections.jsonl").read_text().splitlines()
    assert Counter(json.loads(x)["kind"] for x in lines) == Counter(inj)
    rec = pd.read_csv(src / "records" / "defects.csv")
    assert rec["tail"].str.match(r"^[JH]-\d{4}$").all()
    wo = pd.read_csv(src / "agencies" / "work_orders.csv")
    assert wo["aircraft"].str.match(r"^([JH] \d{4}|[jh]\d{4})$").all()
