"""Integration layer end to end on a short FleetSim run: simulate -> four source exports ->
resolve / link -> store -> reconcile against truth; then the API and signed HUMS ingest.

    python -m pytest -q tests/test_integration.py
"""
import argparse
import json

import pytest
from fastapi.testclient import TestClient

from airpower.fleetsim.__main__ import run as fleetsim_run
from airpower.ingest import load as L
from airpower.ingest.resolve import PartResolver, TailResolver, norm_serial, normalize_text
from airpower.ingest.secure import SignedJson, default_key
from server.app import create_app
from tools import reconcile


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    d = tmp_path_factory.mktemp("run")
    run = d / "r"
    fleetsim_run(argparse.Namespace(config="default", seed=5, policy="P0", days=150, out=str(run),
                                    no_real_signals=True))
    url = f"sqlite:///{(d / 'store.sqlite').as_posix()}"
    L.load(run, url)
    return run, url


def test_resolvers():
    t = TailResolver(["J1001", "J1019", "H2003"])
    assert t("J-1001") == ("J1001", "format") and t("j1019")[0] == "J1019" and t("2003") == ("H2003", "number_only")
    assert t("9999") == (None, "unknown") and t(None) == (None, "missing")
    assert norm_serial("hpO0412") == "HP00412" and norm_serial("HP412") == "HP00412"
    assert normalize_text("ENG HYD PRESS LOW") == "engine hydraulic pressure low"
    import pandas as pd
    items = pd.DataFrame(dict(stock_no=["S1", "S2"], part_no=["HP-3015-02", None]))
    sup = pd.DataFrame(dict(old_part_no=["HP-3015-02"], new_part_no=["HP-3015-03"]))
    pr = PartResolver(items, sup)
    assert pr("hp301502") == ("HP-3015-03", "superseded") and pr("HP-3015-03") == ("HP-3015-03", None)
    assert pr.from_stock("S1") == "HP-3015-03" and pr.unmapped_stock == ["S2"]


def test_every_injected_inconsistency_is_handled(loaded):
    run, url = loaded
    r = reconcile.score(run, url)
    for kind, v in r["injections"].items():
        if v["injected"]:
            assert v["rate"] >= 0.95, (kind, v)
    c = r["counts"]
    assert c["sorties"]["truth"] == c["sorties"]["loaded"] and c["work_orders"]["truth"] == c["work_orders"]["loaded"]
    assert abs(c["defects"]["truth"] - c["defects"]["loaded_unique"]) <= max(3, 0.002 * c["defects"]["truth"])
    h = r["hums_sortie_links"]
    assert h["correct"] / h["records"] > 0.99


def test_serviceability_from_records_matches_truth(loaded):
    run, url = loaded
    s = reconcile.score(run, url)["serviceability"]
    assert s["state_agreement"] > 0.95
    assert abs(s["availability_truth"] - s["availability_derived"]) < 0.03


def test_api_views(loaded):
    _, url = loaded
    c = TestClient(create_app(url, auth=False, warm=False))
    rd = c.get("/api/readiness").json()
    assert sum(u["aircraft"] for u in rd["units"]) == 48
    assert {"serviceable", "nmcs", "nmcm", "scheduled"} <= set(rd["units"][0])
    assert len(c.get("/api/availability?days=7").json()) == 7 * 3
    a = c.get("/api/aircraft/J-1001").json()
    assert a["aircraft"]["tail"] == "J1001" and a["configuration"]
    assert c.get("/api/aircraft/X9").status_code == 404
    dq = c.get("/api/dq").json()
    assert {i["rule"] for i in dq["issues"]} >= {"duplicate_record", "missing_ata", "part_number_format"}


def test_signed_hums_ingest(loaded):
    run, url = loaded
    key = default_key("test-key")
    c = TestClient(create_app(url, hums_key=key, auth=False, warm=False))
    line = next((run / "sources" / "hums").glob("*/*.jsonl")).read_text().splitlines()[0]
    rec = json.loads(line)
    rec["flight_start_utc"] = "2030-01-01T03:00:00Z"            # a new flight from the same unit
    rec["flight_end_utc"] = "2030-01-01T04:00:00Z"
    tx = SignedJson(key)
    sealed = tx.seal(rec)
    r = c.post("/api/ingest/hums", content=sealed)
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["indicators"] > 0
    assert c.post("/api/ingest/hums", content=sealed).status_code == 401          # replay
    assert c.post("/api/ingest/hums", content=tx.seal(rec)).json()["status"] == "duplicate"
    forged = SignedJson(default_key("attacker")).seal(rec)
    assert c.post("/api/ingest/hums", content=forged).status_code == 401
    bad = dict(rec, hums_unit="HU-0000")
    assert c.post("/api/ingest/hums", content=tx.seal(bad)).status_code == 422
