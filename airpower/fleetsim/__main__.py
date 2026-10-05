"""FleetSim command line.

    python -m airpower.fleetsim run     [--days 365] [--seed 1] [--policy P0] [--out DIR] [--no-real-signals]
    python -m airpower.fleetsim compare [--reps 10] [--days 365] [--policies P0 P1] [--procs 4]

`run` writes DIR/sources/ (the four fragmented source systems, what the platform ingests),
DIR/truth/ (what really happened, for scoring only) and DIR/meta.json.
`compare` runs paired replications (same seeds) of each policy and writes
outputs/fleetsim/compare_<policies>.json with means and 95 % confidence intervals.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from airpower.config import FLEETSIM_OUT

from . import simulate


def run(a):
    t0 = time.time()
    sim = simulate(a.config, seed=a.seed, policy=a.policy, real_signals=not a.no_real_signals, days=a.days)
    t_sim = time.time() - t0
    out = Path(a.out or FLEETSIM_OUT / f"{a.policy.lower()}_s{a.seed}_{sim.days}d")
    from . import exports, truth
    _, k = truth.write(sim, out / "truth")
    inj = exports.write(sim, out / "sources")
    meta = dict(config=a.config, seed=a.seed, policy=a.policy, days=sim.days, start_date=str(sim.start),
                utc_offset_h=sim.cfg.utc_offset_h, real_signals=sim.bank.real, signal_notes=sim.bank.notes,
                sim_seconds=round(t_sim, 1), injections=inj, config_resolved=sim.cfg.raw)
    (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    f = k["by_unit"]["FLEET"]
    print(f"{out}  ({t_sim:.0f} s simulated, {time.time() - t0:.0f} s total)")
    print(f"  availability {f['availability']:.1%}  NMCS {f['nmcs']:.1%}  NMCM {f['nmcm']:.1%}  "
          f"scheduled {f['scheduled']:.1%}  task met {f['task_met']:.1%}")
    print(f"  {k['flying']['sorties']} sorties, {k['flying']['fh']:.0f} FH, {k['defects']['total']} defects, "
          f"removals {k['removals']}")
    print(f"  injected inconsistencies: {sum(inj.values())} {inj}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="default")
        p.add_argument("--days", type=int, default=None)
        p.add_argument("--no-real-signals", action="store_true")
        if name == "run":
            p.add_argument("--seed", type=int, default=1)
            p.add_argument("--policy", default="P0")
            p.add_argument("--out", default=None)
        else:
            p.add_argument("--reps", type=int, default=10)
            p.add_argument("--policies", nargs="+", default=["P0", "P1"])
            p.add_argument("--procs", type=int, default=4)
            p.add_argument("--warmup", type=int, default=365, help="days simulated before KPIs are measured")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run(a)
    from .study import compare
    return compare(a)


if __name__ == "__main__":
    sys.exit(main())
