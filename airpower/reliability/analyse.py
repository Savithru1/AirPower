"""A6 Weibull per part, A8 rogue serials and repeat defects, A9 reliability KPIs; from the store.

    python -m airpower.reliability.analyse build [--db URL]          -> reliability tables in the store
    python -m airpower.reliability.analyse evaluate RUN_DIR [--db]   -> RUN_DIR/reliability_evaluation.json

Definitions (component hours = hours flown by the aircraft while the unit was fitted):
* unscheduled removal = failure, inspection finding or no-fault-found removal;
* MTBUR = component hours / unscheduled removals; MTBF = component hours / confirmed failures
  (failures + findings; NFF excluded, using the stores system's repair findings);
* rogue serial = a serial whose unscheduled removals are improbable for its part number:
  P(N >= n | Poisson(part rate x serial's component hours)) < ROGUE_P with n >= 2. A rogue unit
  keeps coming back because repair does not remove the root cause (it may pass every shop test);
* repeat defect = the same aircraft, the same ATA chapter, similar text, recurring within
  REPEAT_DAYS of the previous occurrence (chronic defects, the usual operator definition).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from rapidfuzz import fuzz
from scipy.stats import poisson
from sqlalchemy import insert, text

from airpower.config import db_url
from airpower.model import schema as S
from airpower.reliability import weibull as W
from airpower.reliability.lifedata import Fleet

ROGUE_P = 0.01
ROGUE_LIFE_FACTOR = 1 / 3         # rule of thumb: a rogue unit's life is a third (or less) of its type's
ROGUE_LOG_LR = 2.3                # likelihood ratio 10:1 for the rogue hypothesis
REPEAT_DAYS = 15                  # common operator rule for repetitive defects (recurrence within 15 days)
REPEAT_SIMILARITY = 70
MIN_WEIBULL_FAILURES = 10        # a Weibull fit on fewer failures is not used (MTBF instead)
UNSCHEDULED = ("failure", "finding", "nff")
CONFIRMED = ("failure", "finding")


def part_table(F, iv):
    """A6 + A9 per part number."""
    rows = []
    fitted_now = F.inst[F.inst["removed"].isna()].groupby("pn").size()
    for pn, g in iv.groupby("pn"):
        if pn is None:
            continue
        unsch = g["outcome"].isin(UNSCHEDULED).sum()
        conf = g["outcome"].isin(CONFIRMED).sum()
        nff = (g["outcome"] == "nff").sum()
        hours = g["exposure_fh"].sum()
        r = dict(pn=pn, installed=int(fitted_now.get(pn, 0)), component_hours=float(hours), removals=int((g["outcome"] != "censored").sum()),
                 unscheduled=int(unsch), confirmed_failures=int(conf), nff=int(nff), findings=int((g["outcome"] == "finding").sum()),
                 mtbur_h=float(hours / unsch) if unsch else None, mtbf_h=float(hours / conf) if conf else None,
                 nff_rate=float(nff / unsch) if unsch else None)
        # Weibull in flight hours (confirmed failures are events; NFF, life, preventive, still fitted are censored),
        # on installations made inside the data window only (observed from new). Units fitted before the data
        # starts cannot be used without bias: those removed have unknown age (dropped), those that survive have
        # a known age (life card), so keeping them keeps the survivors of a cohort and loses its failures.
        ok = g["entry_fh"].eq(0)
        w = W.fit(g.loc[ok, "exit_fh"], g.loc[ok, "outcome"].isin(CONFIRMED).astype(int), g.loc[ok, "entry_fh"])
        if w and w["failures"] >= MIN_WEIBULL_FAILURES:
            r.update(weibull_beta=w["beta"], weibull_eta_h=w["eta"], weibull_beta_lo=w["beta_ci"][0], weibull_beta_hi=w["beta_ci"][1],
                     weibull_eta_lo=w["eta_ci"][0], weibull_eta_hi=w["eta_ci"][1], weibull_n=w["n"], weibull_failures=w["failures"])
        # the same in landings, on installations made inside the window (entry known = 0)
        new = g["entry_fh"].eq(0)
        wl = W.fit(g.loc[new, "exposure_landings"], g.loc[new, "outcome"].isin(CONFIRMED).astype(int))
        if wl and wl["failures"] >= MIN_WEIBULL_FAILURES:
            r.update(weibull_beta_landings=wl["beta"], weibull_eta_landings=wl["eta"])
        rows.append(r)
    P = pd.DataFrame(rows).merge(F.parts[["pn", "description", "ata", "category", "unit_cost_inr"]], on="pn", how="left")
    return P.sort_values("unscheduled", ascending=False)


def rogues(iv, parts):
    """A8: serials whose lives look like a rogue unit's rather than a normal unit of their type.

    For each serial, the likelihood of its observed lives (installations made inside the window:
    failures as events, everything else censored) under its part number's fitted Weibull is
    compared with the same Weibull at a third of the scale. A wear-out part that keeps failing
    soon after every repair is very unlikely under the first and likely under the second. The
    simpler Poisson count test (removals vs component hours) is kept alongside for parts without
    a Weibull fit."""
    g = iv[iv["pn"].notna()].copy()
    g["unsch"] = g["outcome"].isin(UNSCHEDULED).astype(int)
    g["event"] = g["outcome"].isin(CONFIRMED).astype(int)
    rate = g.groupby("pn").apply(lambda d: d["unsch"].sum() / max(d["exposure_fh"].sum(), 1e-9), include_groups=False)
    wb = parts.set_index("pn")[["weibull_beta", "weibull_eta_h"]].dropna()
    s = g.groupby(["pn", "sn"]).agg(removals=("unsch", "sum"), component_hours=("exposure_fh", "sum"),
                                    nff=("outcome", lambda o: int((o == "nff").sum())),
                                    tails=("tail", lambda t: ",".join(sorted(set(t)))),
                                    last_removal=("end", "max")).reset_index()
    s["expected"] = s["pn"].map(rate) * s["component_hours"]
    s["p_value"] = poisson.sf(s["removals"] - 1, s["expected"].clip(lower=1e-9))
    new = g[g["entry_fh"].eq(0)]
    llr = {}
    for (pn, sn), d in new.groupby(["pn", "sn"]):
        if pn not in wb.index:
            continue
        b, eta = wb.loc[pn, "weibull_beta"], wb.loc[pn, "weibull_eta_h"]
        t, e = d["exit_fh"].clip(lower=1e-3).to_numpy(), d["event"].to_numpy()

        def ll(scale):
            z = (t / scale) ** b
            return float(np.sum(e * (np.log(b / scale) + (b - 1) * np.log(t / scale))) - np.sum(z))
        llr[(pn, sn)] = ll(eta * ROGUE_LIFE_FACTOR) - ll(eta)
    s["log_lr"] = [llr.get((p, n), np.nan) for p, n in zip(s["pn"], s["sn"])]
    by_lr = s["log_lr"].notna()
    s["rogue"] = (s["removals"] >= 2) & np.where(by_lr, s["log_lr"] > ROGUE_LOG_LR, s["p_value"] < ROGUE_P)
    return s.sort_values(["rogue", "log_lr"], ascending=[False, False])


def repeats(F):
    """A8: chains of similar defects on one aircraft and ATA chapter within REPEAT_DAYS."""
    d = pd.read_sql(text("select defect_no, tail, raised, ata, text_norm from defect where duplicate_of is null and ata is not null"),
                    F.eng)
    d["raised"] = pd.to_datetime(d["raised"])
    d = d.sort_values(["tail", "ata", "raised"]).reset_index(drop=True)
    chain = np.full(len(d), -1)
    nxt = 0
    win = pd.Timedelta(days=REPEAT_DAYS)
    for _, g in d.groupby(["tail", "ata"]):
        idx = list(g.index)
        for k, i in enumerate(idx):
            for j in reversed(idx[:k]):                 # most recent earlier occurrence first
                if d.at[i, "raised"] - d.at[j, "raised"] > win:
                    break
                if fuzz.token_set_ratio(d.at[i, "text_norm"], d.at[j, "text_norm"]) >= REPEAT_SIMILARITY:
                    if chain[j] < 0:
                        chain[j] = nxt
                        nxt += 1
                    chain[i] = chain[j]
                    break
    d["chain"] = chain
    d["repeat"] = False
    c = d[d["chain"] >= 0]
    first = c.groupby("chain")["raised"].transform("min")
    d.loc[c.index, "repeat"] = (c["raised"] > first).to_numpy()
    chains = (c.groupby("chain").agg(tail=("tail", "first"), ata=("ata", "first"), occurrences=("defect_no", "size"),
                                     first=("raised", "min"), last=("raised", "max"), text=("text_norm", "first"),
                                     defects=("defect_no", lambda x: ",".join(x))).reset_index())
    return d, chains.sort_values("occurrences", ascending=False)


def fleet_kpis(F, iv):
    q = lambda sql: pd.read_sql(text(sql), F.eng)          # noqa: E731
    de = q("select count(*) n from defect where duplicate_of is null")["n"][0]
    wo = q("select opened, started, closed, wo_type from work_order where closed is not null")
    for c in ("opened", "closed"):
        wo[c] = pd.to_datetime(wo[c])
    un = wo[wo["wo_type"] == "UNSCHEDULED"]
    tx = q("select demand_no, date, txn_type from stock_txn where demand_no is not null")
    tx["date"] = pd.to_datetime(tx["date"])
    dm = tx[tx["txn_type"] == "DEMAND"].groupby("demand_no")["date"].min()
    isd = tx[tx["txn_type"] == "ISSUE"].groupby("demand_no")["date"].min()
    delay = (isd.reindex(dm.index) - dm).dt.days.dropna()
    u = iv["outcome"].isin(UNSCHEDULED).sum()
    return dict(fleet_flight_hours=F.fleet_fh, defects=int(de), mfhbd_h=float(F.fleet_fh / max(de, 1)),
                unscheduled_removals=int(u), mfhbur_h=float(F.fleet_fh / max(u, 1)),
                nff_share=float((iv["outcome"] == "nff").sum() / max(u, 1)),
                mttr_unscheduled_h_median=float(((un["closed"] - un["opened"]).dt.total_seconds() / 3600).median()),
                logistics_delay_days_mean=float(delay.mean()) if len(delay) else None,
                demands_waiting_over_7_days=float((delay > 7).mean()) if len(delay) else None)


def build(url=None):
    F = Fleet(url or db_url())
    iv = F.intervals()
    P = part_table(F, iv)
    R = rogues(iv, P)
    d, chains = repeats(F)
    K = fleet_kpis(F, iv)
    md = S.Base.metadata.tables
    for t in ("reliability_part", "rogue_candidate", "repeat_defect", "meta"):
        md[t].create(F.eng, checkfirst=True)
    def recs(df):
        return df.astype(object).where(df.notna(), None).to_dict("records")
    with F.eng.begin() as c:
        for t in ("reliability_part", "rogue_candidate", "repeat_defect"):
            c.execute(md[t].delete())
        cols = [x.name for x in md["reliability_part"].columns]
        c.execute(insert(md["reliability_part"]), recs(P[[x for x in cols if x in P.columns]]))
        rc = R[R["rogue"]].drop(columns=["rogue", "log_lr"])
        if len(rc):
            c.execute(insert(md["rogue_candidate"]), recs(rc.assign(last_removal=rc["last_removal"].dt.to_pydatetime())))
        if len(chains):
            c.execute(insert(md["repeat_defect"]), recs(chains.rename(columns={"chain": "chain_id"})))
        c.execute(text("delete from meta where key = 'fleet_kpis'"))
        c.execute(insert(md["meta"]), [dict(key="fleet_kpis", value=json.dumps(K))])
    return dict(parts=P, rogues=R, defects=d, chains=chains, kpis=K, intervals=iv)


def evaluate(run_dir, url=None):
    """Against FleetSim truth: Weibull recovery, rogue precision / recall, repeat-defect detection."""
    import yaml
    run = Path(run_dir)
    out = build(url)
    meta = json.loads((run / "meta.json").read_text())
    cfg = meta["config_resolved"]
    from airpower.fleetsim.config import load as load_cfg
    fc = load_cfg("default")
    spec = {pn: specs[0] for pn, specs in fc.parts().items()}
    P = out["parts"]
    rec = []
    for r in P.itertuples():
        s = spec.get(r.pn)
        if s is None or not s.weibull or (s.hums and s.hums["kind"] in ("cmapss", "battery")):
            continue
        b_true, e_true = s.weibull
        if s.clock == "cycles":
            b_fit, e_fit = getattr(r, "weibull_beta_landings", np.nan), getattr(r, "weibull_eta_landings", np.nan)
        else:
            b_fit, e_fit = getattr(r, "weibull_beta", np.nan), getattr(r, "weibull_eta_h", np.nan)
        if pd.isna(e_fit):
            continue
        rec.append(dict(pn=r.pn, clock=s.clock, policy=s.policy, pf=s.pf, failures=int(r.confirmed_failures),
                        beta_true=b_true, beta_fit=float(b_fit), eta_true=e_true, eta_fit=float(e_fit),
                        eta_ratio=float(e_fit / e_true), beta_ratio=float(b_fit / b_true)))
    Wd = pd.DataFrame(rec)
    sr = pd.read_parquet(run / "truth" / "serials.parquet")[["sn", "rogue"]]
    R = out["rogues"].merge(sr.rename(columns={"rogue": "true_rogue"}), on="sn", how="left")
    R["true_rogue"] = R["true_rogue"].fillna(False).astype(bool)
    flagged = R["rogue"]
    detectable = R["true_rogue"] & (R["removals"] >= 2)
    truth_def = pd.read_parquet(run / "truth" / "defects.parquet")[["id", "repeat", "kind"]]
    xw = pd.read_parquet(run / "truth" / "crosswalk.parquet")
    xw = xw[xw["entity"] == "defect"][["truth_id", "source_id"]]
    D = out["defects"].merge(xw, left_on="defect_no", right_on="source_id").merge(truth_def, left_on="truth_id", right_on="id")
    D["true_repeat"] = D["repeat"].fillna(False).astype(bool) if "repeat_y" not in D else D["repeat_y"].fillna(False).astype(bool)
    D["found"] = D["repeat_x"] if "repeat_x" in D else D["repeat"]
    res = dict(
        weibull=dict(parts=int(len(Wd)), median_eta_ratio=float(Wd["eta_ratio"].median()) if len(Wd) else None,
                     median_beta_ratio=float(Wd["beta_ratio"].median()) if len(Wd) else None,
                     within_25pct_eta=float((abs(Wd["eta_ratio"] - 1) <= 0.25).mean()) if len(Wd) else None,
                     per_part=Wd.round(3).to_dict("records")),
        rogue=dict(serials=int(len(R)), true_rogues=int(R["true_rogue"].sum()), flagged=int(flagged.sum()),
                   precision=float(R.loc[flagged, "true_rogue"].mean()) if flagged.any() else None,
                   recall_all_rogues=float(R.loc[R["true_rogue"], "rogue"].mean()) if R["true_rogue"].any() else None,
                   recall_rogues_removed_twice=float(R.loc[detectable, "rogue"].mean()) if detectable.any() else None),
        repeat_defects=dict(defects=int(len(D)), true_repeats=int(D["true_repeat"].sum()), flagged=int(D["found"].sum()),
                            precision=float(D.loc[D["found"], "true_repeat"].mean()) if D["found"].any() else None,
                            recall=float(D.loc[D["true_repeat"], "found"].mean()) if D["true_repeat"].any() else None,
                            note="truth repeats are recurrences of chronic snags; coincidental same-text snags on one aircraft "
                                 "within 30 days count as false positives here although a maintainer would also chase them"),
        fleet_kpis=out["kpis"])
    (run / "reliability_evaluation.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "evaluate"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        out = build(a.db)
        print(json.dumps(out["kpis"], indent=1))
        print(out["parts"][["pn", "description", "unscheduled", "mtbur_h", "mtbf_h", "nff_rate", "weibull_beta", "weibull_eta_h"]].head(12).round(2).to_string())
        print(f"rogue candidates: {int(out['rogues']['rogue'].sum())}; repeat-defect chains: {len(out['chains'])}")
    else:
        r = evaluate(a.run_dir, a.db)
        print(json.dumps({k: (v if k != "weibull" else {kk: vv for kk, vv in v.items() if kk != "per_part"}) for k, v in r.items()},
                         indent=1, default=str))
    return 0


if __name__ == "__main__":
    from airpower.reliability import analyse as _m
    sys.exit(_m.main())
