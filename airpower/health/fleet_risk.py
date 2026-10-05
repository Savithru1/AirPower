"""A5 and the risk records: health models applied to the integrated fleet store.

For every HUMS-monitored component the builder writes risk records (table `risk_record`): the
component's remaining life with an interval where a model gives one, the probability it must
be removed within the next 100 flight hours, a health index, a status and an explanation.
The planner, the spares forecast and the dashboard read risk only from this table.

* engine_rul       - the C-MAPSS trend model (engine_rul.py) on each engine's HUMS trend
                     snapshots, consolidated per trend cycle (registry hums_config). Engine age
                     comes from the technical records: life-card hours since overhaul minus the
                     hours flown since (fitted units), or hours since installation.
* gearbox_anomaly  - each gearbox against its own healthy baseline (its first records), EWMA
                     z-scores of the four vibration features, thresholds and a probability
                     calibration fitted on a separate training run (A3 showed that baselining is
                     what makes detection transfer across operating conditions).
* battery_rul      - robust trend of state of health to end of life (battery.py), interval from
                     the leave-one-cell-out relative error.

    python -m airpower.health.fleet_risk fit-gearbox outputs/fleetsim/train_s2 --db sqlite:///outputs/train_s2.sqlite
    python -m airpower.health.fleet_risk build [--db URL]
    python -m airpower.health.fleet_risk evaluate outputs/fleetsim/p0_s1_365d [--db URL]
"""
import argparse
import json
import math
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import yaml
from scipy.stats import norm
from sqlalchemy import insert, text

from airpower.config import OUTPUTS, db_url
from airpower.health import artifacts
from airpower.health import detect as D
from airpower.model import schema as S

REGISTRY = Path(__file__).resolve().parents[1] / "ingest" / "registry.yaml"
MODELS = OUTPUTS / "models"
HORIZON_FH = 100.0
CMAPSS_CHANNELS = ["op1", "op2", "op3", "T2", "T24", "T30", "T50", "P2", "P15", "P30", "Nf", "Nc", "epr", "Ps30",
                   "phi", "NRf", "NRc", "BPR", "farB", "htBleed", "Nf_dmd", "PCNfR_dmd", "W31", "W32"]
CMAPSS_COLS = ["setting1", "setting2", "setting3"] + [f"s{i}" for i in range(1, 22)]
VIB = ["Vib_RMS", "Vib_Kurtosis", "crest", "env_spec_kurt"]
BASELINE_N = 25
PERSIST_N = 3


def pos_key(p):
    return str(p).replace("_", "-").replace("#", "-")


def theil_sen(x, y):
    """Median of pairwise slopes and the matching intercept (Theil-Sen, without scipy's interval
    computations, which dominate the run time when called once per record)."""
    i, j = np.triu_indices(len(x), 1)
    dx = x[j] - x[i]
    ok = dx > 1e-9
    if not ok.any():
        return 0.0, float(np.median(y))
    sl = float(np.median((y[j] - y[i])[ok] / dx[ok]))
    return sl, float(np.median(y) - sl * np.median(x))      # same intercept convention as scipy


def status_of(p, alarm=False):
    if alarm or (p is not None and p >= 0.5):
        return "alert"
    if p is not None and p >= 0.2:
        return "watch"
    return "ok"


# ================================================================== inputs
class Inputs:
    def __init__(self, url):
        self.eng = S.engine(url)
        q = lambda sql: pd.read_sql(text(sql), self.eng)          # noqa: E731
        self.hi = q("select ts, hums_record_id, tail, position, sn, channel, value from health_indicator")
        self.hi["ts"] = pd.to_datetime(self.hi["ts"])
        so = q("select tail, landing, fh from sortie order by landing")
        so["landing"] = pd.to_datetime(so["landing"])
        self.cum = {t: (g["landing"].astype("int64").to_numpy() / 3.6e12, g["fh"].cumsum().to_numpy())
                    for t, g in so.groupby("tail")}
        self.end = so["landing"].max()
        inst = q("select tail, position, pn, sn, installed, removed, tso_h_at_export from installation")
        for c in ("installed", "removed"):
            inst[c] = pd.to_datetime(inst[c])
        self.inst = inst
        self.types = q("select tail, type_id from aircraft").set_index("tail")["type_id"].to_dict()
        self.reg = yaml.safe_load(REGISTRY.read_text())["hums_config"]

    def fh_between(self, tail, t0, t1):
        """Flight hours flown by `tail` between two times (vectorised over t0 / t1 arrays)."""
        x, c = self.cum[tail]
        f = lambda t: np.interp(pd.to_datetime(t).astype("int64") / 3.6e12, x, c, left=0.0)   # noqa: E731
        return f(t1) - f(t0)

    def ages(self, tail, sn, ts):
        """Age (FH since overhaul / installation) of serial `sn` on `tail` at times ts; NaN if unknown."""
        ts = pd.to_datetime(pd.Series(ts)).reset_index(drop=True)
        out = np.full(len(ts), np.nan)
        rows = self.inst[(self.inst["tail"] == tail) & (self.inst["sn"] == sn)]
        for r in rows.itertuples():
            lo = r.installed if pd.notna(r.installed) else pd.Timestamp.min
            hi = r.removed if pd.notna(r.removed) else pd.Timestamp.max
            m = ((ts >= lo) & (ts < hi)).to_numpy()
            if not m.any():
                continue
            if pd.notna(r.installed):
                out[m] = self.fh_between(tail, pd.Series([r.installed] * m.sum()), ts[m])
            elif pd.isna(r.removed) and pd.notna(r.tso_h_at_export):
                out[m] = r.tso_h_at_export - self.fh_between(tail, ts[m], pd.Series([self.end] * m.sum()))
        return out

    def wide(self, positions):
        h = self.hi[self.hi["position"].isin(positions)]
        w = h.pivot_table(index=["hums_record_id", "ts", "tail", "position", "sn"], columns="channel", values="value")
        return w.reset_index().sort_values("ts")


# ================================================================== engines
def engine_risk(I):
    from airpower.health.engine_rul import EngineRUL                  # noqa: F401 (needed to unpickle)
    w = I.wide(["ENG", "ENG_1", "ENG_2"])
    if w.empty or not set(CMAPSS_CHANNELS) <= set(w.columns):
        return pd.DataFrame()
    out = []
    for typ, subset in I.reg["engine_model"].items():
        model = artifacts.load(MODELS / f"engine_rul_{subset}.joblib")
        hpc = I.reg["engine_trend_cycle_fh"][typ]
        frames, meta = [], {}
        for (tail, pos, sn), g in w[w["tail"].map(I.types) == typ].groupby(["tail", "position", "sn"]):
            g = g.reset_index(drop=True)
            age = I.ages(tail, sn, g["ts"])
            known = np.isfinite(age).all()
            rel = I.fh_between(tail, pd.Series([g["ts"].iloc[0]] * len(g)), g["ts"])
            cyc = np.floor((age if known else rel) / hpc).astype(int)
            g = g.assign(cycle=cyc, age=age if known else np.nan)
            b = g.groupby("cycle").agg({**{c: "mean" for c in CMAPSS_CHANNELS}, "ts": "max", "age": "max"}).reset_index()
            unit = f"{tail}|{pos}|{sn}"
            fr = b.rename(columns=dict(zip(CMAPSS_CHANNELS, CMAPSS_COLS))).assign(unit=unit)
            fr["cycles_seen"] = fr["cycle"] + 1 if known else np.nan
            frames.append(fr)
            meta[unit] = (tail, pos, sn, known)
        if not frames:
            continue
        F = pd.concat(frames, ignore_index=True)
        P = model.predict_frame(F[["unit", "cycle", "cycles_seen"] + CMAPSS_COLS])
        P = P.merge(F[["unit", "cycle", "ts", "age"]], on=["unit", "cycle"])
        sigma = np.maximum((P["hi"] - P["lo"]) / 2 / 1.645, 1.0)
        P["p"] = norm.cdf((HORIZON_FH / hpc - P["rul"]) / sigma)
        last = F.groupby("unit").tail(1)
        expl = dict(zip(last["unit"], model.explain(F[["unit", "cycle", "cycles_seen"] + CMAPSS_COLS])))
        last_cycle = dict(zip(last["unit"], last["cycle"]))
        for r in P.itertuples():
            tail, pos, sn, known = meta[r.unit]
            ex = dict(model=f"C-MAPSS {subset} trend model", age_from_records=bool(known))
            if r.unit in expl and r.cycle == last_cycle[r.unit]:
                ex["drivers"] = expl[r.unit]
            out.append(dict(tail=tail, position=pos_key(pos), sn=sn, kind="engine_rul", as_of=r.ts,
                            age_fh=None if pd.isna(r.age) else float(r.age), rul_fh=float(r.rul * hpc),
                            rul_fh_lo=float(r.lo * hpc), rul_fh_hi=float(r.hi * hpc), p_fail_100fh=float(r.p),
                            health_index=float(1 - min(r.rul, 125) / 125), status=status_of(float(r.p)),
                            explanation=json.dumps(ex), model=f"engine_rul_{subset}"))
    return pd.DataFrame(out)


# ================================================================== gearboxes
def gearbox_z(I):
    """Per-serial baselined, EWMA-smoothed z-scores of the vibration features."""
    w = I.wide(I.reg["gearbox_positions"])
    if w.empty:
        return w
    w["Vib_RMS"] = np.log(w["Vib_RMS"])
    parts = []
    for (tail, pos, sn), g in w.groupby(["tail", "position", "sn"]):
        g = g.reset_index(drop=True)
        base = g.head(BASELINE_N)
        mu = base[VIB].mean()
        sd = np.maximum(base[VIB].std(), np.maximum(0.05 * mu.abs(), 1e-6))
        z = (g[VIB] - mu) / sd
        z["active"] = True
        z.loc[: BASELINE_N - 1, "active"] = False                      # baseline records are not scored
        s = D.smoothed(z, VIB)
        parts.append(pd.concat([g[["hums_record_id", "ts", "tail", "position", "sn"]], z.add_prefix("z_")[[f"z_{c}" for c in VIB]],
                                s.add_prefix("s_"), z[["active"]]], axis=1))
    return pd.concat(parts, ignore_index=True)


def gearbox_hi(zs, th):
    ratio = np.stack([np.abs(zs[f"s_{c}"].to_numpy()) / th[c] for c in VIB], axis=1)
    hi = np.nan_to_num(ratio).max(axis=1)
    hi[~zs["active"].to_numpy()] = np.nan
    return hi, ratio


def gearbox_risk(I):
    p = MODELS / "gearbox_detector.json"
    if not p.exists():
        return pd.DataFrame()
    cal = json.loads(p.read_text())
    zs = gearbox_z(I)
    if zs.empty:
        return pd.DataFrame()
    hi, ratio = gearbox_hi(zs, cal["thresholds"])
    zs["hi"] = hi
    out = []
    for (tail, pos, sn), g in zs.groupby(["tail", "position", "sn"]):
        flag = (g["hi"].fillna(0) >= 1.0).to_numpy()
        alarm = D.persist(flag, PERSIST_N)
        for i, r in enumerate(g.itertuples()):
            if not r.active:
                continue
            x = cal["calibration"]
            pr = 1 / (1 + math.exp(-(x["intercept"] + x["coef"] * r.hi)))
            k = int(np.argmax(ratio[g.index[i]]))
            ex = dict(model="per-serial vibration baseline", top_feature=VIB[k],
                      sigma_above_baseline=round(float(getattr(r, f"s_{VIB[k]}")), 2), threshold_sigma=round(cal["thresholds"][VIB[k]], 2))
            out.append(dict(tail=tail, position=pos_key(pos), sn=sn, kind="gearbox_anomaly", as_of=r.ts, age_fh=None,
                            rul_fh=None, rul_fh_lo=None, rul_fh_hi=None, p_fail_100fh=float(pr), health_index=float(r.hi),
                            status=status_of(float(pr), bool(alarm[i])), explanation=json.dumps(ex), model="gearbox_detector"))
    return pd.DataFrame(out)


def fit_gearbox(run_dir, url, target_fa_per_100fh=2.0):
    """Thresholds (leave-one-aircraft-out margin) and P(removal within 100 FH | HI) on a TRAINING run."""
    I = Inputs(url)
    zs = gearbox_z(I)
    tr = truth_join(run_dir, zs.rename(columns={"position": "pos"}).assign(position=lambda d: d["pos"]))
    tr["healthy"] = tr["age"] < tr["p_point"]
    groups, hours = {}, {}
    for tail, g in tr.groupby("tail"):
        tables = []
        for _, s in g[g["healthy"]].groupby("sn"):
            if len(s) > 10:
                z = s[[f"z_{c}" for c in VIB]].rename(columns=lambda c: c[2:]).assign(active=s["active"].to_numpy())
                sm = s[[f"s_{c}" for c in VIB]].rename(columns=lambda c: c[2:])
                tables.append((z.reset_index(drop=True), sm.reset_index(drop=True)))
        if tables:
            groups[tail] = tables
            hours[tail] = 1.7 * sum(len(t[0]) for t in tables)            # ~1.7 FH per helicopter sortie
    sel = D.select_margin(groups, VIB, hours, target_fa_per_100h=target_fa_per_100fh, floor=3.0, n=PERSIST_N)
    maxima = [D.healthy_maxima(sm, VIB, PERSIST_N) for tabs in groups.values() for _, sm in tabs]
    th = D.thresholds_from_maxima(maxima, VIB, floor=3.0, margin=sel["chosen_margin"])
    hi, _ = gearbox_hi(tr, th)
    tr["hi"] = hi
    d = tr[tr["active"]].dropna(subset=["hi"])
    # positive = a fault is developing: inside the P-F window (detectable, removal due) or within
    # 100 FH of failure. Gearboxes are mostly caught at inspection inside the window, so failure
    # itself is rarely approached.
    y = ((d["age"] >= d["p_point"]) | ((d["ttf"] - d["age"]) <= HORIZON_FH)).astype(int)
    from sklearn.linear_model import LogisticRegression
    lr = LogisticRegression().fit(d[["hi"]], y)
    res = dict(thresholds=th, margin_selection=sel, calibration=dict(intercept=float(lr.intercept_[0]), coef=float(lr.coef_[0][0])),
               training_run=str(run_dir), records=int(len(d)), positives=int(y.sum()))
    MODELS.mkdir(parents=True, exist_ok=True)
    (MODELS / "gearbox_detector.json").write_text(json.dumps(res, indent=1))
    return res


# ================================================================== batteries
def battery_risk(I):
    w = I.wide([I.reg["battery_position"]])
    if w.empty:
        return pd.DataFrame()
    eol = I.reg["battery_eol_soh"]
    pred = OUTPUTS / "health" / "battery_predictions.csv"
    if pred.exists():                             # 90 % quantile of |ln(predicted / true)|, leave-one-cell-out (A4)
        bp = pd.read_csv(pred)
        qlog = float(np.quantile(np.abs(np.log(np.maximum(bp["trend"], 1.0) / bp["true"])), 0.9))
    else:
        qlog = 1.0
    out = []
    for (tail, pos, sn), g in w.groupby(["tail", "position", "sn"]):
        g = g.reset_index(drop=True)
        x = I.fh_between(tail, pd.Series([g["ts"].iloc[0]] * len(g)), g["ts"])
        soh = g["soh"].to_numpy()
        for i in range(9, len(g)):
            lo_i = max(0, i - 29)
            xs, ys = x[lo_i:i + 1], soh[lo_i:i + 1]
            if np.ptp(xs) < 1e-6:
                continue
            sl, ic = theil_sen(xs, ys)
            level = ic + sl * xs[-1]
            rul = float(np.clip((eol - level) / sl, 0, 5000)) if sl < 0 else 5000.0
            sig = qlog / 1.645
            p = float(norm.cdf((math.log(HORIZON_FH) - math.log(max(rul, 1.0))) / sig))
            out.append(dict(tail=tail, position=pos_key(pos), sn=sn, kind="battery_rul", as_of=g["ts"].iloc[i], age_fh=None,
                            rul_fh=rul, rul_fh_lo=rul * math.exp(-qlog), rul_fh_hi=rul * math.exp(qlog), p_fail_100fh=p,
                            health_index=float(np.clip((1 - level) / (1 - eol), 0, 1.5)), status=status_of(p),
                            explanation=json.dumps(dict(model="state-of-health trend", soh=round(float(level), 3),
                                                        fade_per_100fh=round(float(-sl * 100), 4))),
                            model="battery_trend"))
    return pd.DataFrame(out)


# ================================================================== build / evaluate
def build(url=None):
    I = Inputs(url or db_url())
    parts = [engine_risk(I), gearbox_risk(I), battery_risk(I)]
    R = pd.concat([p for p in parts if len(p)], ignore_index=True)
    pn = I.inst.assign(position=I.inst["position"].map(pos_key)).drop_duplicates(["tail", "sn"]).set_index(["tail", "sn"])["pn"]
    R["pn"] = [pn.get((t, s)) for t, s in zip(R["tail"], R["sn"])]
    R.insert(0, "id", np.arange(1, len(R) + 1))
    md = S.Base.metadata.tables
    md["risk_record"].create(I.eng, checkfirst=True)
    with I.eng.begin() as c:
        c.execute(md["risk_record"].delete())
        recs = R.astype(object).where(R.notna(), None).to_dict("records")
        for i in range(0, len(recs), 20000):
            c.execute(insert(md["risk_record"]), recs[i:i + 20000])
    return R


def truth_join(run_dir, df):
    """Attach the true age / ttf / P point to rows keyed by (sn, ts = HUMS flight end, UTC)."""
    run = Path(run_dir)
    meta = json.loads((run / "meta.json").read_text())
    ht = pd.read_parquet(run / "truth" / "hums_truth.parquet")
    start = pd.Timestamp(meta["start_date"])
    ht["ts"] = (start + pd.to_timedelta(ht["t"] - meta["utc_offset_h"], unit="h")).dt.floor("s")
    df = df.copy()
    df["ts_key"] = pd.to_datetime(df["ts"] if "ts" in df else df["as_of"]).dt.floor("s")
    return df.merge(ht[["sn", "ts", "age", "ttf", "p_point", "fault_label"]].rename(columns={"ts": "ts_key"}),
                    on=["sn", "ts_key"], how="inner")


def evaluate(run_dir, url=None):
    eng = S.engine(url or db_url())
    R = pd.read_sql(text("select * from risk_record"), eng)
    R["as_of"] = pd.to_datetime(R["as_of"])
    J = truth_join(run_dir, R)
    J["true_rul"] = J["ttf"] - J["age"]
    reg = yaml.safe_load(REGISTRY.read_text())["hums_config"]
    res = {}
    for kind, g in J.groupby("kind"):
        r = dict(records=int(len(g)), components=int(g["sn"].nunique()))
        if kind == "engine_rul":
            cap = 125 * min(reg["engine_trend_cycle_fh"].values())
            m = g["true_rul"] <= cap
            d = g["rul_fh"] - g["true_rul"]
            r.update(rmse_fh_all=float(np.sqrt(np.mean(d ** 2))), rmse_fh_last_cap=float(np.sqrt(np.mean(d[m] ** 2))),
                     coverage90_last_cap=float(((g["rul_fh_lo"] <= g["true_rul"]) & (g["true_rul"] <= g["rul_fh_hi"]))[m].mean()),
                     age_known_share=float(g["age_fh"].notna().mean()))
        if kind == "battery_rul":
            m = g["true_rul"] <= 300
            rel = (g["rul_fh"] - g["true_rul"]) / g["true_rul"]
            r.update(alpha_lambda_a03_near_eol=float((np.abs(rel[m]) <= 0.3).mean()),
                     coverage90_near_eol=float(((g["rul_fh_lo"] <= g["true_rul"]) & (g["true_rul"] <= g["rul_fh_hi"]))[m].mean()))
        # flagging: a component is "degrading" once its true age passes its P point (the fault is
        # physically detectable; the fleet removes most such units at inspection before failure).
        # A life is detected if the component was flagged (watch or alert) while degrading.
        g = g.assign(degrading=g["age"] >= g["p_point"], flagged=g["status"] != "ok")
        lives, hit, leads = 0, 0, []
        for (sn, ttf), s in g.groupby(["sn", "ttf"]):
            dg = s[s["degrading"]].sort_values("as_of")
            if not len(dg):
                continue
            lives += 1
            f = dg[dg["flagged"]]
            if len(f):
                hit += 1
                leads.append(float(f["true_rul"].iloc[0]))
        healthy = g[~g["degrading"]]
        r.update(degrading_lives=lives, flagged_while_degrading=hit, detection_rate=hit / lives if lives else None,
                 median_true_rul_at_first_flag_fh=float(np.median(leads)) if leads else None,
                 false_flag_share_healthy=float(healthy["flagged"].mean()) if len(healthy) else None,
                 brier_100fh=float(np.mean((g["p_fail_100fh"] - ((g["true_rul"] <= HORIZON_FH) | g["degrading"])) ** 2)))
        # horizon view, the same for every model: of the records whose removal is truly due within
        # 100 FH, how many are flagged; of the flags, how many are within 200 FH of the end
        due = g["true_rul"] <= HORIZON_FH
        r.update(recall_due_within_100fh=float(g.loc[due, "flagged"].mean()) if due.any() else None,
                 precision_flag_within_200fh=float((g.loc[g["flagged"], "true_rul"] <= 2 * HORIZON_FH).mean())
                 if g["flagged"].any() else None,
                 base_rate_due_within_100fh=float(due.mean()))
        if kind == "engine_rul":
            k = g["age_fh"].notna()
            d = g["rul_fh"] - g["true_rul"]
            cap = g["tail"].str[0].map(reg["engine_trend_cycle_fh"]) * 125
            inr = g["true_rul"] <= cap
            r.update(bias_fh_age_known=float(d[inr & k].mean()), bias_fh_age_unknown=float(d[inr & ~k].mean()))
        res[kind] = r
    (Path(run_dir) / "risk_evaluation.json").write_text(json.dumps(res, indent=1))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fit-gearbox", "build", "evaluate"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "fit-gearbox":
        r = fit_gearbox(a.run_dir, a.db)
        print(json.dumps({k: v for k, v in r.items() if k != "margin_selection"}, indent=1))
    elif a.cmd == "build":
        R = build(a.db)
        print(f"risk records: {len(R)}; latest status per component:")
        last = R.sort_values("as_of").groupby(["tail", "position", "kind"]).tail(1)
        print(last.groupby(["kind", "status"]).size().to_string())
    else:
        print(json.dumps(evaluate(a.run_dir, a.db), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
