"""NASA C-MAPSS turbofan run-to-failure data: loader and the train / fleet-replay split.

FleetSim replays C-MAPSS training trajectories as the HUMS signal of engine serials. If the
RUL model were trained on the same trajectories, the fleet study would test it on its own
training data. So the run-to-failure units of the subsets FleetSim uses are split once, with a
fixed seed, into
    'train' - may be used to fit models (Phase 3), and
    'fleet' - replayed by FleetSim only, never used for fitting.
The official test sets (test_FD00x + RUL_FD00x) stay the external benchmark.

    python -m data_prep.cmapss extract     # unzip data/raw/cmapss/cmapss.zip if needed
    python -m data_prep.cmapss split       # write data/processed/cmapss/split.json
    python -m data_prep.cmapss summary
"""
import argparse
import io
import json
import sys
import zipfile

import numpy as np
import pandas as pd

from airpower.config import PROCESSED, RAW

DIR = RAW / "cmapss"
SPLIT = PROCESSED / "cmapss" / "split.json"
SETTINGS = ["setting1", "setting2", "setting3"]
SENSORS = [f"s{i}" for i in range(1, 22)]
COLS = ["unit", "cycle"] + SETTINGS + SENSORS
SUBSETS = ("FD001", "FD002", "FD003", "FD004")
FLEET_SUBSETS = ("FD001", "FD003")      # single operating condition, like a cruise snapshot
FLEET_FRACTION = 0.4
SPLIT_SEED = 26249


def load(subset="FD001", part="train"):
    """part 'train': run-to-failure units, rul = cycles remaining to failure.
    part 'test': truncated units, rul from RUL_FD00x (true RUL at the last cycle)."""
    d = pd.read_csv(DIR / f"{part}_{subset}.txt", sep=r"\s+", header=None, names=COLS, engine="python")
    last = d.groupby("unit")["cycle"].transform("max")
    if part == "train":
        d["rul"] = last - d["cycle"]
    else:
        end = pd.read_csv(DIR / f"RUL_{subset}.txt", header=None, names=["rul_end"])
        end["unit"] = np.arange(1, len(end) + 1)
        d = d.merge(end, on="unit")
        d["rul"] = d["rul_end"] + last - d["cycle"]
        d = d.drop(columns="rul_end")
    d["subset"] = subset
    return d


def extract():
    """Unzip the NASA archive (the outer zip may contain a nested CMAPSSData.zip)."""
    if (DIR / "train_FD001.txt").exists():
        return "present"

    def walk(zf):
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if base.endswith(".zip"):
                walk(zipfile.ZipFile(io.BytesIO(zf.read(name))))
            elif base.endswith(".txt") and base[:4] in ("trai", "test", "RUL_", "read"):
                (DIR / base).write_bytes(zf.read(name))

    walk(zipfile.ZipFile(DIR / "cmapss.zip"))
    return "extracted"


def make_split(seed=SPLIT_SEED, frac=FLEET_FRACTION):
    rng = np.random.default_rng(seed)
    out = {"seed": seed, "fleet_fraction": frac, "subsets": {}}
    for sub in FLEET_SUBSETS:
        units = np.sort(load(sub, "train")["unit"].unique())
        fleet = np.sort(rng.choice(units, size=int(round(frac * len(units))), replace=False))
        out["subsets"][sub] = {"fleet": [int(u) for u in fleet],
                               "train": [int(u) for u in units if u not in set(fleet)]}
    SPLIT.parent.mkdir(parents=True, exist_ok=True)
    SPLIT.write_text(json.dumps(out, indent=1))
    return out


def split():
    """The persisted split (created on first use)."""
    return json.loads(SPLIT.read_text()) if SPLIT.exists() else make_split()


def fleet_trajectories(subset):
    """Run-to-failure trajectories reserved for FleetSim: {unit: DataFrame sorted by cycle}."""
    d = load(subset, "train")
    keep = set(split()["subsets"][subset]["fleet"])
    return {int(u): g.sort_values("cycle").reset_index(drop=True) for u, g in d.groupby("unit") if u in keep}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["extract", "split", "summary"])
    a = ap.parse_args(argv)
    if a.cmd == "extract":
        print(extract())
    elif a.cmd == "split":
        s = make_split()
        for sub, v in s["subsets"].items():
            print(f"{sub}: {len(v['train'])} train units, {len(v['fleet'])} fleet-replay units")
    else:
        for sub in SUBSETS:
            tr, te = load(sub, "train"), load(sub, "test")
            life = tr.groupby("unit")["cycle"].max()
            print(f"{sub}: train {tr['unit'].nunique()} units (life {life.min()}-{life.max()} cycles, "
                  f"median {int(life.median())}), test {te['unit'].nunique()} units")
    return 0


if __name__ == "__main__":
    sys.exit(main())
