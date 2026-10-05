"""The real datasets load from the AirPower repository (C-MAPSS locally, the rest from
AeroTwin's prepared data). Skipped where a dataset is not present on this machine.

    python -m pytest -q tests/test_data_sources.py
"""
import pytest

from data_prep import cmapss, sources

pytestmark = pytest.mark.data


def _need(sid):
    if not sources.available(sid):
        pytest.skip(f"{sid} not available")


def test_cmapss_train_and_test_rul():
    _need("cmapss")
    tr = cmapss.load("FD001", "train")
    assert tr["unit"].nunique() == 100 and (tr.groupby("unit")["rul"].min() == 0).all()
    te = cmapss.load("FD001", "test")
    assert te["unit"].nunique() == 100 and (te["rul"] >= 0).all()
    assert list(tr.columns[:5]) == ["unit", "cycle", "setting1", "setting2", "setting3"]


def test_cmapss_fleet_split_is_disjoint_and_stable():
    _need("cmapss")
    s = cmapss.split()
    for sub in cmapss.FLEET_SUBSETS:
        fleet, train = set(s["subsets"][sub]["fleet"]), set(s["subsets"][sub]["train"])
        assert fleet and train and not fleet & train and len(fleet | train) == 100
    assert cmapss.make_split() == s                                      # seeded: re-creating gives the same split
    traj = cmapss.fleet_trajectories("FD001")
    assert set(traj) == set(s["subsets"]["FD001"]["fleet"])
    assert all(t["rul"].iloc[-1] == 0 for t in traj.values())


def test_vibration_windows():
    _need("vibration")
    w = sources.load_vibration_windows()
    assert {"cwru_bearing", "seu_gearbox"} <= set(w["dataset"])
    assert {"Vib_RMS", "Vib_Kurtosis", "label"} <= set(w.columns)
    assert "healthy" in set(w["label"])


def test_battery_cycles():
    _need("nasa_battery")
    b = sources.load_battery_cycles()
    assert {"cell", "cycle", "capacity_ah", "soh"} <= set(b.columns)
    assert b.groupby("cell")["soh"].min().min() < 0.8


def test_ngafid_labels_and_one_flight():
    _need("ngafid_2days")
    lab = sources.load_ngafid_labels("ngafid_2days")
    assert len(lab) == 11446 and set(lab["before_after"]) == {0, 1} and lab["fold"].nunique() == 5
    fid = int(lab.index[0])
    f = sources.read_ngafid_flights("ngafid_2days", [fid], columns=["E1_RPM", "E1_Oil_Pressure"])
    assert len(f) > 100 and set(f["flight_id"]) == {fid}
