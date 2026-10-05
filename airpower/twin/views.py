"""Aircraft twin views over the store (Phase 7): fleet health map, tail page, recommended actions.

Everything here reads the integrated store only. The aircraft twin is the as-maintained state:
which serial is in which position, life consumed against its limits, the latest health risk of
every monitored component, inspections due, open and past defects, and the actions the models
and the planner recommend, each with the reason it was raised.

`q` is a query function returning a DataFrame (server.app.Store.q), so the views share the
API's connection and date handling.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from airpower.spares.forecast import parse_limit

REGISTRY = Path(__file__).resolve().parents[1] / "ingest" / "registry.yaml"
OK, WATCH, ALERT = 0, 1, 2
SYSTEMS = [  # column, positions (store names without the -n suffix), applies to types
    ("Engines", ["ENG"], "JH"),
    ("Gearboxes", ["MGB", "IGB", "TGB"], "H"),
    ("Rotor blades", ["MRB", "TRB"], "H"),
    ("Battery", ["BAT"], "JH"),
    ("Escape system", ["CART"], "J"),
]
KIND_LABEL = {"engine_rul": "engine trend (C-MAPSS model)", "gearbox_anomaly": "vibration vs own baseline",
              "battery_rul": "state-of-health trend"}


def _base(pos):
    return str(pos).rsplit("-", 1)[0] if str(pos)[-1:].isdigit() and "-" in str(pos) else str(pos)


class Twin:
    """Loads what the views need once per request (the store is small)."""

    def __init__(self, q, at=None):
        self.q = q
        self.reg = yaml.safe_load(REGISTRY.read_text())
        st = q('select tail, start, "end", status, reason from aircraft_status')
        self.end = st["end"].max()
        self.at = pd.Timestamp(at) if at is not None else self.end
        cur = st[(st["start"] <= self.at) & (st["end"] > self.at)]
        self.status = cur.set_index("tail")[["status", "reason", "start"]]
        self.ac = q("select tail, msn, type_id, unit_id, base, hums_unit from aircraft").set_index("tail")
        so = q("select tail, landing, fh from sortie order by landing")
        self.cum = {t: (g["landing"].astype("int64").to_numpy() / 3.6e12, g["fh"].cumsum().to_numpy())
                    for t, g in so.groupby("tail")}
        rec = so[so["landing"] > self.at - pd.Timedelta(days=60)]
        rec = rec[rec["landing"] <= self.at]
        self.usage = (rec.groupby("tail")["fh"].sum() / 60).reindex(self.ac.index).fillna(0.1).clip(lower=0.1)
        inst = q("select tail, position, pn, sn, installed, removed, tso_h_at_export, life_limit from installation")
        fitted = inst[(inst["installed"].isna() | (inst["installed"] <= self.at)) & (inst["removed"].isna() | (inst["removed"] > self.at))]
        self.fitted = fitted.copy()
        self.parts = q("select pn, description, category from part").set_index("pn")

    def fh_between(self, tail, t0, t1):
        if tail not in self.cum:
            return 0.0
        x, c = self.cum[tail]
        f = lambda t: float(np.interp(pd.Timestamp(t).value / 3.6e12, x, c, left=0.0))   # noqa: E731
        return f(t1) - f(t0)

    # ------------------------------------------------------------------ life-limited parts
    def life(self, tail=None):
        rows = []
        f = self.fitted if tail is None else self.fitted[self.fitted["tail"] == tail]
        for r in f.itertuples():
            lim = parse_limit(r.life_limit)
            if not lim:
                continue
            u = float(self.usage.get(r.tail, 0.1))
            fh_since = self.fh_between(r.tail, self.at, self.end)
            age = None
            if pd.notna(r.tso_h_at_export) and pd.isna(r.removed):
                age = float(r.tso_h_at_export) - fh_since
            elif pd.notna(r.installed):
                age = self.fh_between(r.tail, r.installed, self.at)
            cons, left_days, basis = [], [], []
            if "fh" in lim and age is not None:
                cons.append(age / lim["fh"])
                left_days.append((lim["fh"] - age) / u)
                basis.append(f"{age:,.0f} of {lim['fh']:,.0f} FH")
            if "days" in lim and pd.notna(r.installed):
                d = (self.at - r.installed).total_seconds() / 86400
                cons.append(d / lim["days"])
                left_days.append(lim["days"] - d)
                basis.append(f"{d:,.0f} of {lim['days']:,.0f} days")
            if not cons:
                rows.append(dict(tail=r.tail, position=r.position, pn=r.pn, sn=r.sn, limit=r.life_limit, consumed=None,
                                 days_left=None, due=None, basis="age not in the records"))
                continue
            c, dl = max(cons), min(left_days)
            rows.append(dict(tail=r.tail, position=r.position, pn=r.pn, sn=r.sn, limit=r.life_limit, consumed=float(c),
                             days_left=float(dl), due=(self.at + pd.Timedelta(days=max(dl, 0))).date().isoformat(),
                             basis="; ".join(basis)))
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ inspections
    def inspections(self, tail=None):
        st = self.q("select tail, name, last_done, last_done_fh, airframe_fh_at_export from inspection_status")
        st["last_done"] = pd.to_datetime(st["last_done"])
        if tail is not None:
            st = st[st["tail"] == tail]
        prog = self.reg["maintenance_programme"]
        rows = []
        for r in st.itertuples():
            typ = self.ac.loc[r.tail, "type_id"]
            p = next((x for x in prog[typ] if x["name"] == r.name), None)
            if p is None:
                continue
            u = float(self.usage.get(r.tail, 0.1))
            af = r.airframe_fh_at_export - self.fh_between(r.tail, self.at, self.end)
            due = []
            if p.get("interval_fh"):
                due.append((p["interval_fh"] - (af - r.last_done_fh)) / u)
            if p.get("interval_days") and pd.notna(r.last_done):
                due.append(p["interval_days"] - (self.at - r.last_done).total_seconds() / 86400)
            d = min(due)
            rows.append(dict(tail=r.tail, inspection=r.name, level=p["level"], last_done=None if pd.isna(r.last_done) else r.last_done.isoformat(),
                             fh_since=float(af - r.last_done_fh), interval_fh=p.get("interval_fh"), interval_days=p.get("interval_days"),
                             days_to_due=float(d), due=(self.at + pd.Timedelta(days=d)).date().isoformat()))
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ risk
    def risk(self, tail=None):
        try:
            sql = "select * from risk_record where as_of <= :at" + (" and tail = :t" if tail else "")
            r = self.q(sql, at=self.at.to_pydatetime(), **({"t": tail} if tail else {}))
        except Exception:
            return pd.DataFrame()
        if not len(r):
            return r
        r["as_of"] = pd.to_datetime(r["as_of"])
        last = r.sort_values("as_of").groupby(["tail", "position", "kind"]).tail(1)
        last = last.merge(self.fitted[["tail", "sn"]].drop_duplicates(), on=["tail", "sn"])
        last["explanation"] = last["explanation"].map(lambda s: json.loads(s) if isinstance(s, str) else None)
        return last


def _level(status):
    return {"ok": OK, "watch": WATCH, "alert": ALERT}.get(status, OK)


def _life_level(c):
    return None if c is None or (isinstance(c, float) and math.isnan(c)) else (ALERT if c >= 1.0 else WATCH if c >= 0.9 else OK)


def health_map(q, at=None):
    """Tails x systems: the worst of HUMS risk and life consumed per system, plus open defects,
    the next inspection and parts at risk of being awaited."""
    T = Twin(q, at)
    risk, life, ins = T.risk(), T.life(), T.inspections()
    de = q("select tail, category, ata, text, raised from defect where duplicate_of is null and raised <= :at and "
           "(closed is null or closed > :at)", at=T.at.to_pydatetime())
    try:
        aog = q("select tail, position, pn, p_within_lead, fleet_stock from aog_risk")
    except Exception:
        aog = pd.DataFrame(columns=["tail", "position", "pn", "p_within_lead", "fleet_stock"])
    by = lambda df: {t: g.to_dict("records") for t, g in df.groupby("tail")} if len(df) else {}   # noqa: E731
    rk, lf, dd, ax = by(risk.drop(columns=["explanation"], errors="ignore")), by(life), by(de), by(aog)
    nxt = ins.sort_values("days_to_due").groupby("tail").head(1).set_index("tail") if len(ins) else pd.DataFrame()
    rows = []
    for tail, a in T.ac.sort_values(["unit_id"]).iterrows():
        cells = {}
        for name, poss, types in SYSTEMS:
            if a["type_id"] not in types:
                cells[name] = None
                continue
            lv, notes = OK, []
            for r in rk.get(tail, []):
                if _base(r["position"]) not in poss:
                    continue
                lv = max(lv, _level(r["status"]))
                if r["status"] != "ok":
                    rul = f", RUL {r['rul_fh']:,.0f} FH" if pd.notna(r["rul_fh"]) else ""
                    notes.append(f"{r['position']}: {r['status']} ({KIND_LABEL.get(r['kind'], r['kind'])}{rul})")
            for r in lf.get(tail, []):
                if _base(r["position"]) not in poss:
                    continue
                l2 = _life_level(r["consumed"])
                if l2 is not None:
                    lv = max(lv, l2)
                    if l2 > OK:
                        notes.append(f"{r['position']}: {100 * r['consumed']:.0f} % of life ({r['basis']})")
            cells[name] = dict(level=lv, note="; ".join(notes) or "no finding")
        d = dd.get(tail, [])
        g = sum(1 for x in d if x["category"] == "U/S")
        cells["Open defects"] = dict(level=ALERT if g else (WATCH if d else OK),
                                     note=f"{len(d)} open ({g} grounding)" if d else "none", n=len(d))
        if tail in nxt.index:
            nx = nxt.loc[tail]
            cells["Next inspection"] = dict(level=ALERT if nx["days_to_due"] < 0 else WATCH if nx["days_to_due"] < 7 else OK,
                                            note=f"{nx['inspection']} in {nx['days_to_due']:.0f} days", days=float(nx["days_to_due"]))
        x = ax.get(tail, [])
        cells["Spares"] = dict(level=(ALERT if any(r["fleet_stock"] == 0 for r in x) else WATCH) if x else OK,
                               note="; ".join(f"{r['position']} {r['pn']}: {100 * r['p_within_lead']:.0f} % within lead time" for r in x)
                               or "no part at risk")
        s = T.status.loc[tail] if tail in T.status.index else None
        rows.append(dict(tail=tail, unit=a["unit_id"], type=a["type_id"], status=None if s is None else s["status"],
                         reason=None if s is None else s["reason"], cells=cells))
    cols = [s[0] for s in SYSTEMS] + ["Open defects", "Next inspection", "Spares"]
    return dict(at=T.at.isoformat(), columns=cols, rows=rows)


def tail_twin(q, tail, at=None):
    """Everything about one aircraft for the tail page and the tail report."""
    T = Twin(q, at)
    if tail not in T.ac.index:
        raise KeyError(tail)
    a = T.ac.loc[tail]
    life = T.life(tail)
    ins = T.inspections(tail)
    risk = T.risk(tail)
    de = q("select defect_no, raised, closed, category, ata, ata_source, text, rectification, wo_id from defect "
           "where tail = :t and duplicate_of is null and raised <= :at order by raised desc", t=tail, at=T.at.to_pydatetime())
    cfg = T.fitted[T.fitted["tail"] == tail].merge(T.parts.reset_index()[["pn", "description"]], on="pn", how="left")
    actions = recommend(q, T, tail, risk, life, ins)
    s = T.status.loc[tail] if tail in T.status.index else None
    by_ata = de.groupby("ata").size().sort_values(ascending=False).head(8)
    return dict(
        aircraft=dict(tail=tail, msn=a["msn"], type=a["type_id"], unit=a["unit_id"], base=a["base"], hums_unit=a["hums_unit"]),
        at=T.at.isoformat(), status=None if s is None else dict(status=s["status"], reason=s["reason"], since=s["start"].isoformat()),
        usage_fh_per_day=float(T.usage.get(tail, 0.1)),
        configuration=cfg[["position", "pn", "description", "sn", "installed", "tso_h_at_export"]].sort_values("position"),
        life=life, inspections=ins, risk=risk, actions=actions,
        defects=de.head(200), defects_by_ata=[dict(ata=None if pd.isna(k) else int(k), n=int(v)) for k, v in by_ata.items()],
        defects_total=int(len(de)))


def recommend(q, T, tail, risk, life, ins):
    """Actions for one aircraft, each with its source and the reason it was raised."""
    out = []
    for r in (risk.itertuples() if len(risk) else []):
        if r.status == "ok":
            continue
        ex = r.explanation or {}
        why = [KIND_LABEL.get(r.kind, r.kind)]
        if pd.notna(r.rul_fh):
            why.append(f"remaining life {r.rul_fh:,.0f} FH (90 % interval {r.rul_fh_lo:,.0f}-{r.rul_fh_hi:,.0f})")
        if pd.notna(r.p_fail_100fh):
            why.append(f"P(removal within 100 FH) {100 * r.p_fail_100fh:.0f} %")
        if ex.get("drivers"):
            why.append("drivers: " + ", ".join(f"{d['feature']} ({d['effect_cycles']:+.0f} cycles)" for d in ex["drivers"][:3]))
        if ex.get("top_feature"):
            why.append(f"{ex['top_feature']} {ex.get('sigma_above_baseline')} sigma above its own baseline")
        if ex.get("soh") is not None:
            why.append(f"state of health {ex['soh']:.2f}, fading {ex.get('fade_per_100fh')} per 100 FH")
        days = (r.rul_fh / max(T.usage.get(tail, 0.1), 0.1)) if pd.notna(r.rul_fh) else None
        out.append(dict(priority=1 if r.status == "alert" else 2, source="HUMS model",
                        action=f"Plan replacement of {r.position} (S/N {r.sn})" + (f" within {days:,.0f} days" if days is not None else ""),
                        why="; ".join(why)))
    for r in (life.itertuples() if len(life) else []):
        if r.consumed is not None and r.days_left is not None and r.days_left < 60:
            out.append(dict(priority=1 if r.days_left < 14 else 2, source="technical records",
                            action=f"{r.position} (S/N {r.sn}) reaches its life limit in {max(r.days_left, 0):,.0f} days: have the spare at base",
                            why=f"life consumed {100 * r.consumed:.0f} % ({r.basis}); limit {r.limit}"))
    try:
        pj = q("select job, kind, start_day, due_day, bundled_into, reason, agency from plan_job where plan = 'optimised' and tail = :t "
               "and start_day is not null order by start_day", t=tail)
        meta = q("select value from meta where key = 'plan'")
        t0 = json.loads(meta["value"][0])["t0"] if len(meta) else None
    except Exception:
        pj, t0 = pd.DataFrame(), None
    for r in (pj.itertuples() if len(pj) else []):
        if r.kind == "blocked":
            continue
        when = (pd.Timestamp(t0) + pd.Timedelta(days=int(r.start_day))).date().isoformat() if t0 else f"day {r.start_day}"
        early = int(r.due_day - r.start_day) if r.due_day is not None and not pd.isna(r.due_day) else 0
        why = "maintenance plan (CP-SAT): " + (r.reason if isinstance(r.reason, str) else "maintenance programme")
        if r.kind == "inspection" and early > 0:
            why += f"; {early} days before due, to keep the flying task / bundle work"
        out.append(dict(priority=3, source="planner", action=f"Induct for {r.job} on {when} ({r.agency})", why=why))
    for r in (q("select position, pn, p_within_lead, lead_days, fleet_stock from aog_risk where tail = :t", t=tail).itertuples()
              if _has(q, "aog_risk") else []):
        out.append(dict(priority=1 if r.fleet_stock == 0 else 2, source="spares model",
                        action=f"{'Expedite' if r.fleet_stock == 0 else 'Transfer'} a spare {r.pn} for {r.position}",
                        why=f"{100 * r.p_within_lead:.0f} % chance it is needed within the {r.lead_days:.0f}-day lead time; "
                            f"{'none in stock anywhere' if r.fleet_stock == 0 else 'none at this base'}"))
    if _has(q, "repeat_defect"):
        for r in q("select ata, occurrences, text from repeat_defect where tail = :t order by occurrences desc limit 3", t=tail).itertuples():
            out.append(dict(priority=2, source="records analytics", action=f"Troubleshoot the repeat defect (ATA {r.ata}) to its root cause",
                            why=f"{r.occurrences} similar reports within 15 days of each other: \"{r.text}\""))
    if _has(q, "rogue_candidate"):
        sns = set(T.fitted.loc[T.fitted["tail"] == tail, "sn"])
        for r in q("select pn, sn, removals, expected from rogue_candidate").itertuples():
            if r.sn in sns:
                out.append(dict(priority=2, source="records analytics", action=f"Replace suspected rogue unit {r.pn} S/N {r.sn} at the next opportunity",
                                why=f"removed {r.removals} times against {r.expected:.1f} expected for its hours"))
    return sorted(out, key=lambda x: x["priority"])


def _has(q, table):
    try:
        q(f"select 1 from {table} limit 1")
        return True
    except Exception:
        return False
