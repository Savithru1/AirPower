"""Paired policy comparison: the same seeds for every policy (common random numbers).

Workers must live in an importable module (not __main__) for Windows process pools.
"""
import json
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from airpower.config import FLEETSIM_OUT

from . import simulate


def _one(args):
    config, policy, seed, days, real, warmup = args
    from . import truth
    sim = simulate(config, seed=seed, policy=policy, real_signals=real, days=days, warmup_days=warmup)
    return policy, seed, truth.kpis(sim, truth.tables(sim))


def compare(a):
    jobs = [(a.config, p, s, a.days, not a.no_real_signals, a.warmup) for s in range(1, a.reps + 1) for p in a.policies]
    t0 = time.time()
    with ProcessPoolExecutor(a.procs) as ex:
        res = list(ex.map(_one, jobs))
    metrics = ["availability", "nmcs", "nmcm", "scheduled", "serviceable_0800", "task_met"]
    out = dict(reps=a.reps, days=a.days, warmup_days=a.warmup, policies=a.policies, seconds=round(time.time() - t0), results={})
    for p in a.policies:
        ks = [k for pp, _, k in res if pp == p]
        row = {}
        for m in metrics:
            v = np.array([k["by_unit"]["FLEET"][m] for k in ks])
            row[m] = dict(mean=float(v.mean()), ci95=float(1.96 * v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 1 else None)
        for m, f in [("in_service_failures", lambda k: k["in_service_failures"]),
                     ("removals", lambda k: sum(k["removals"].values())),
                     ("nff_removals", lambda k: k["removals"].get("nff", 0)),
                     ("preventive_removals", lambda k: k["removals"].get("preventive", 0)),
                     ("backordered_demands", lambda k: k["spares"]["backordered"]),
                     ("material_cost_lakh", lambda k: k["spares"]["material_cost_lakh"]),
                     ("labour_h", lambda k: k["labour_h"])]:
            v = np.array([f(k) for k in ks], dtype=float)
            row[m] = dict(mean=float(v.mean()), ci95=float(1.96 * v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 1 else None)
        out["results"][p] = row
    if len(a.policies) == 2:                                 # paired difference (common seeds)
        p, q = a.policies
        kp = {s: k for pp, s, k in res if pp == p}
        kq = {s: k for pp, s, k in res if pp == q}
        d = np.array([kq[s]["by_unit"]["FLEET"]["availability"] - kp[s]["by_unit"]["FLEET"]["availability"] for s in kp])
        out["paired_availability_diff"] = dict(of=f"{q} - {p}", mean=float(d.mean()),
                                               ci95=float(1.96 * d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 1 else None)
    path = FLEETSIM_OUT / f"compare_{'_'.join(a.policies)}_{a.days or 365}d_{a.reps}r.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    for p in a.policies:
        r = out["results"][p]
        print(f"{p}: availability {r['availability']['mean']:.1%} +- {r['availability']['ci95'] or 0:.1%}, "
              f"NMCS {r['nmcs']['mean']:.1%}, NMCM {r['nmcm']['mean']:.1%}, scheduled {r['scheduled']['mean']:.1%}, "
              f"task met {r['task_met']['mean']:.1%}, removals {r['removals']['mean']:.0f}, "
              f"failures {r['in_service_failures']['mean']:.0f}")
    if "paired_availability_diff" in out:
        d = out["paired_availability_diff"]
        print(f"paired difference {d['of']}: {d['mean']:+.2%} +- {d['ci95'] or 0:.2%}")
    print(f"-> {path}  ({out['seconds']} s)")
    return 0


