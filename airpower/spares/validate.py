"""Does the METRIC allocation buy more aircraft in the simulated fleet, for the same money?

The platform computes METRIC stock levels from a store's records (rates, pipelines, fleet
structure). Those levels are then imposed as the initial stock of fresh FleetSim fleets (seeds
never used before) and compared, paired by seed, with the simulator's own stocking rule (Poisson
quantile per part), which is what the stores held in the records. Policy P0 in both arms; only
the stock differs. A second METRIC arm at a lower budget shows what money it saves.

    python -m airpower.spares.validate --db URL [--reps 20] [--procs 4]
"""
import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
from sqlalchemy import text

from airpower.config import OUTPUTS
from airpower.ingest.resolve import key
from airpower.model import schema as S

SEED0 = 101


def _run(args):
    seed, override, days, warmup = args
    from airpower.fleetsim import simulate, truth
    ov = {"stores": {"stock_override": override}} if override else None
    sim = simulate(seed=seed, policy="P0", days=days, warmup_days=warmup, overrides=ov)
    k = truth.kpis(sim, truth.tables(sim))
    value = sum(sim.stores.sizing[pn]["stock"] * sim.parts[pn][0].cost for pn in sim.parts)     # INR lakh
    return seed, k["by_unit"]["FLEET"], {u: k["by_unit"][u]["availability"] for u in k["by_unit"] if u != "FLEET"}, value


def to_sim_pn(stock, url):
    """Platform P/Ns (current numbers after supersession) -> the simulator's part numbers."""
    from airpower.fleetsim.config import load as load_cfg
    alias = pd.read_sql(text("select alias, pn from part_alias"), S.engine(url)).set_index("alias")["pn"].to_dict()
    out = {}
    for pn in load_cfg().parts():
        plat = alias.get(key(pn), pn)
        if plat in stock:
            out[pn] = int(stock[plat])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--warmup", type=int, default=365)
    ap.add_argument("--budget-fraction", type=float, default=0.75)
    a = ap.parse_args(argv)
    from airpower.spares.metric import Inputs, Metric
    inp = Inputs(a.db)
    M = Metric(inp)
    base = inp.baseline_stock().reindex(M.skus).fillna(0).astype(int)
    base_cost = float(sum(base[pn] * M.cost[pn] for pn in M.skus))
    pts = M.curve(max_cost=1.2 * base_cost)
    arms = {"baseline (simulator rule)": None,
            "METRIC, same budget": to_sim_pn(M.at_budget(pts, base_cost)["stock"], a.db),
            f"METRIC, {int(a.budget_fraction * 100)} % budget": to_sim_pn(M.at_budget(pts, a.budget_fraction * base_cost)["stock"], a.db)}
    jobs = [(SEED0 + i, ov, a.days, a.warmup) for name, ov in arms.items() for i in range(a.reps)]
    t0 = time.time()
    with ProcessPoolExecutor(a.procs) as ex:
        res = list(ex.map(_run, jobs))
    out = dict(store=a.db, reps=a.reps, days=a.days, warmup_days=a.warmup, seconds=round(time.time() - t0), arms={})
    names = list(arms)
    for i, name in enumerate(names):
        r = res[i * a.reps:(i + 1) * a.reps]
        av = np.array([x[1]["availability"] for x in r])
        out["arms"][name] = dict(stock_value_lakh=float(np.mean([x[3] for x in r])),
                                 availability=float(av.mean()), ci95=float(1.96 * av.std(ddof=1) / np.sqrt(len(av))),
                                 nmcs=float(np.mean([x[1]["nmcs"] for x in r])),
                                 task_met=float(np.mean([x[1]["task_met"] for x in r])),
                                 by_unit={u: float(np.mean([x[2][u] for x in r])) for u in r[0][2]},
                                 per_seed=[float(v) for v in av])
    b = np.array(out["arms"][names[0]]["per_seed"])
    for name in names[1:]:
        d = np.array(out["arms"][name]["per_seed"]) - b
        out["arms"][name]["paired_diff_vs_baseline"] = dict(mean=float(d.mean()), ci95=float(1.96 * d.std(ddof=1) / np.sqrt(len(d))))
    (OUTPUTS / "spares").mkdir(parents=True, exist_ok=True)
    (OUTPUTS / "spares" / "metric_validation.json").write_text(json.dumps(out, indent=1))
    for name, v in out["arms"].items():
        d = v.get("paired_diff_vs_baseline")
        print(f"{name:28s} stock INR {v['stock_value_lakh']:7.0f} lakh  availability {v['availability']:.1%} +- {v['ci95']:.1%}  "
              f"NMCS {v['nmcs']:.1%}" + (f"  paired diff {d['mean']:+.2%} +- {d['ci95']:.2%}" if d else ""))
    print(f"({out['seconds']} s)")
    return 0


if __name__ == "__main__":
    from airpower.spares import validate as _m
    sys.exit(_m.main())
