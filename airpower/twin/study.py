"""Phase 6: the policy study in the simulated fleet (plan section 7) and the value-of-prediction curve.

Every arm runs on the same seeds (common random numbers, with one random stream per aircraft, per
part number and for the flying task, so that arms stay in step), which are never used to build any
model (seeds 201+; models and the METRIC allocation come from seeds 2 / 3 and the real datasets). Each run
is a one-year measurement after a one-year warm-up under the same policy. The measured year has the
same two exercises as the warm-up year (flying task x1.4 for 14 days, x1.5 for 10 days).

Arms
  P0           reactive, stock by the simulator's own rule for P0 (what the stores held)
  P1           preventive, stock by the same rule sized for P1's removal rate (more stock)
  P2           predictive: the models as built, the weekly CP-SAT plan, P0's stock
  P0+METRIC    P0 with the METRIC allocation of the same budget (Phase 5)
  P2+METRIC    P2 with the METRIC allocation of the same budget
Value of prediction and the parts of P2 (P2 timing removals by rules, without the CP-SAT plan,
which gives the same availability at a third of the run time; P2-rules is the curve's "as built"):
  P2-rules           models as built
  P2-rules x0 ... x3 each model's error x k (0 = perfect foresight)
  P2-rules records   no HUMS predictions (life limits and demand rates from the records only)
  P2-rules no positioning   without prediction-driven spares positioning

Runs are appended to <out>_runs.jsonl as they finish; a rerun skips the (arm, seed) pairs already
there, so a long study can be resumed.

    python -m airpower.twin.study [--reps 30] [--procs 10] [--arms P0 P2 ...] [--metric-db URL]
"""
import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

from airpower.config import OUTPUTS

SEED0 = 201
OUT = OUTPUTS / "twin"

ARMS = {
    "P0": dict(policy="P0"),
    "P1": dict(policy="P1"),
    "P2": dict(policy="P2", args=dict()),
    "P0+METRIC": dict(policy="P0", stock="metric"),
    "P2+METRIC": dict(policy="P2", args=dict(), stock="metric"),
    "P2-rules": dict(policy="P2", args=dict(planner=None)),
    "P2-rules x0": dict(policy="P2", args=dict(planner=None, quality=0.0)),
    "P2-rules x1.5": dict(policy="P2", args=dict(planner=None, quality=1.5)),
    "P2-rules x2": dict(policy="P2", args=dict(planner=None, quality=2.0)),
    "P2-rules x3": dict(policy="P2", args=dict(planner=None, quality=3.0)),
    "P2-rules records": dict(policy="P2", args=dict(planner=None, hums=False)),
    "P2-rules no positioning": dict(policy="P2", args=dict(planner=None, position=False)),
}
METRICS = ["availability", "task_met", "nmcs", "nmcm", "scheduled", "serviceable_0800"]
COUNTS = ["in_service_failures", "unscheduled_removals", "removals", "nff_removals", "predictive_removals",
          "preventive_removals", "aborted_sorties", "backordered_demands", "material_cost_lakh", "labour_h",
          "stock_value_lakh", "early_inspections"]


def measured_year_surges():
    from airpower.fleetsim.config import load
    sur = load().raw["surges"]
    return sur + [dict(s, start_day=s["start_day"] + 365) for s in sur]


def _one(job):
    name, arm, seed, days, warmup, stock = job
    from airpower.fleetsim import simulate, truth
    ov = {"surges": measured_year_surges(), "crn_streams": True, "log_hums": False}
    if stock:
        ov["stores"] = {"stock_override": stock}
    t0 = time.time()
    sim = simulate(seed=seed, policy=arm["policy"], policy_args=arm.get("args"), days=days, warmup_days=warmup, overrides=ov)
    tab = truth.tables(sim, hums=False)
    k = truth.kpis(sim, tab)
    f = k["by_unit"]["FLEET"]
    rm = k["removals"]
    ins = tab["inspections"]
    ins = ins[ins["t_opened"] >= sim.warmup * 24.0]
    row = {m: f[m] for m in METRICS}
    row.update(in_service_failures=k["in_service_failures"], removals=sum(rm.values()),
               unscheduled_removals=rm.get("failure", 0) + rm.get("nff", 0), nff_removals=rm.get("nff", 0),
               predictive_removals=rm.get("predictive", 0), preventive_removals=rm.get("preventive", 0),
               aborted_sorties=k["flying"]["aborted"], backordered_demands=k["spares"]["backordered"],
               material_cost_lakh=k["spares"]["material_cost_lakh"], labour_h=k["labour_h"],
               stock_value_lakh=float(sum(sim.stores.sizing[pn]["stock"] * sim.parts[pn][0].cost for pn in sim.parts)),
               early_inspections=int(ins["early"].sum()) if "early" in ins else 0,
               by_unit={u: v["availability"] for u, v in k["by_unit"].items() if u != "FLEET"},
               seconds=round(time.time() - t0, 1))
    pol = sim.policy.summary() if hasattr(sim.policy, "summary") else {}
    return name, seed, row, pol


def metric_stock(url):
    """The METRIC allocation of the current budget, from a training store (Phase 5), in simulator P/Ns."""
    from airpower.spares.metric import Inputs, Metric
    from airpower.spares.validate import to_sim_pn
    inp = Inputs(url)
    M = Metric(inp)
    base = inp.baseline_stock().reindex(M.skus).fillna(0).astype(int)
    budget = float(sum(base[pn] * M.cost[pn] for pn in M.skus))
    return to_sim_pn(M.at_budget(M.curve(max_cost=1.2 * budget), budget)["stock"], url)


def _ci(v):
    v = np.asarray(v, float)
    return dict(mean=float(v.mean()), ci95=float(1.96 * v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 1 else None)


def aggregate(res, names, reps, days, warmup, seconds):
    out = dict(reps=reps, days=days, warmup_days=warmup, seed0=SEED0, seconds=seconds,
               arms={n: dict(ARMS.get(n, {}), **{"stock": ARMS.get(n, {}).get("stock") or "simulator rule for the policy"})
                     for n in names}, results={}, paired_vs_P0={}, policy={})
    by = {n: {s: r for nn, s, r, _ in res if nn == n} for n in names}
    pol = {n: [p for nn, _, _, p in res if nn == n] for n in names}
    for n in names:
        rows = list(by[n].values())
        out["results"][n] = {m: _ci([r[m] for r in rows]) for m in METRICS + COUNTS}
        out["results"][n]["by_unit"] = {u: float(np.mean([r["by_unit"][u] for r in rows])) for u in rows[0]["by_unit"]}
        out["results"][n]["seconds_per_run"] = float(np.mean([r["seconds"] for r in rows]))
        if pol[n] and pol[n][0]:
            keys = [k for k, v in pol[n][0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            out["policy"][n] = {k: float(np.mean([p[k] for p in pol[n] if k in p])) for k in keys}
    if "P0" in by:
        for n in names:
            if n == "P0":
                continue
            seeds = sorted(set(by[n]) & set(by["P0"]))
            out["paired_vs_P0"][n] = {m: _ci([by[n][s][m] - by["P0"][s][m] for s in seeds])
                                      for m in ("availability", "task_met", "nmcs", "nmcm", "scheduled")}
    if "P2" in by and "P0+METRIC" in by and "P2+METRIC" in by:
        seeds = sorted(set(by["P2+METRIC"]) & set(by["P0+METRIC"]))
        out["paired_P2_vs_P0_at_METRIC_stock"] = _ci([by["P2+METRIC"][s]["availability"] - by["P0+METRIC"][s]["availability"]
                                                      for s in seeds])
    for a, b in (("P2", "P2-rules"), ("P2-rules", "P2-rules records"), ("P2-rules", "P2-rules no positioning"), ("P2-rules x0", "P2-rules")):
        if a in by and b in by:
            seeds = sorted(set(by[a]) & set(by[b]))
            out[f"paired_{a}_vs_{b}"] = _ci([by[a][s]["availability"] - by[b][s]["availability"] for s in seeds])
    out["value_of_prediction"] = vop_curve(out)
    return out


def vop_curve(out):
    """Availability gain over P0 against the prediction error multiple (k): perfect (0), as built (1),
    degraded (1.5, 2, 3), and none (records only)."""
    pts = []
    for n, k in (("P2-rules x0", 0.0), ("P2-rules", 1.0), ("P2-rules x1.5", 1.5), ("P2-rules x2", 2.0), ("P2-rules x3", 3.0),
                 ("P2-rules records", None)):
        if n in out["paired_vs_P0"]:
            d = out["paired_vs_P0"][n]["availability"]
            q = out["policy"].get(n, {})
            pts.append(dict(arm=n, error_multiple=k, gain=d["mean"], ci95=d["ci95"],
                            engine_rmse_fh=q.get("engine_rmse_fh_last125"), gearbox_auc=q.get("gearbox_auc_degrading"),
                            in_service_failures=out["results"][n]["in_service_failures"]["mean"],
                            predictive_removals=out["results"][n]["predictive_removals"]["mean"]))
    return pts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--procs", type=int, default=10)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--warmup", type=int, default=365)
    ap.add_argument("--arms", nargs="+", default=list(ARMS))
    ap.add_argument("--metric-db", default="sqlite:///outputs/risk_s2.sqlite", help="training store for the METRIC allocation")
    ap.add_argument("--out", default="policy_study.json")
    a = ap.parse_args(argv)
    stock = metric_stock(a.metric_db) if any(ARMS[n].get("stock") for n in a.arms) else None
    jobs = [(n, ARMS[n], SEED0 + i, a.days, a.warmup, stock if ARMS[n].get("stock") else None)
            for i in range(a.reps) for n in a.arms]
    jobs.sort(key=lambda j: (j[1]["policy"] != "P2", (j[1].get("args") or {}).get("planner", "cpsat") is None))  # slow arms first
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / a.out.replace(".json", "_runs.jsonl")
    res = []
    if log.exists():                                   # resume: keep finished runs of the same study
        for line in log.read_text().splitlines():
            x = json.loads(line)
            if x["arm"] in a.arms and x["seed"] < SEED0 + a.reps:
                res.append((x["arm"], x["seed"], x["row"], x["policy"]))
    done = {(n, s) for n, s, _, _ in res}
    todo = [j for j in jobs if (j[0], j[2]) not in done]
    print(f"{len(done)} runs already done, {len(todo)} to run", flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(a.procs) as ex, log.open("a") as f:
        futs = [ex.submit(_one, j) for j in todo]
        for k, fu in enumerate(as_completed(futs), 1):
            n, s, row, pol = fu.result()
            res.append((n, s, row, pol))
            f.write(json.dumps(dict(arm=n, seed=s, row=row, policy=pol), default=float) + "\n")
            f.flush()
            if k % 20 == 0:
                print(f"  {k}/{len(todo)} runs, {time.time() - t0:.0f} s", flush=True)
    out = aggregate(res, a.arms, a.reps, a.days, a.warmup, round(time.time() - t0))
    (OUT / a.out).write_text(json.dumps(out, indent=1))
    for n in a.arms:
        r = out["results"][n]
        d = out["paired_vs_P0"].get(n, {}).get("availability")
        print(f"{n:24s} availability {r['availability']['mean']:.1%} +- {r['availability']['ci95'] or 0:.1%}  "
              f"NMCS {r['nmcs']['mean']:.1%}  task met {r['task_met']['mean']:.1%}  failures {r['in_service_failures']['mean']:.0f}  "
              f"removals {r['removals']['mean']:.0f}  stock {r['stock_value_lakh']['mean'] / 100:.0f} cr"
              + (f"  vs P0 {d['mean']:+.2%} +- {d['ci95']:.2%}" if d else ""))
    print(f"({out['seconds']} s) -> {OUT / a.out}")
    return 0


if __name__ == "__main__":
    from airpower.twin import study as _m
    sys.exit(_m.main())
