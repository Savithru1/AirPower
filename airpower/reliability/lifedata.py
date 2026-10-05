"""Life data from the integrated store: one row per installation of a serial, with its exposure.

Each installation (table `installation`, rebuilt from the records' component changes and life
cards) becomes an interval of flight hours and landings flown by its aircraft:

* entry   - hours since overhaul at the start of the interval: 0 for a unit installed inside the
            data window (repaired rotables return as new; new units are new); for a unit already
            fitted when the data starts and still fitted at the end, its life-card hours minus
            the hours flown since; otherwise unknown (the interval is dropped from Weibull fits
            but still counts as exposure for rates);
* exit    - entry + hours flown while fitted;
* outcome - how the interval ended, from the records' change reason and, for unscheduled
            removals, the stores system's repair return for that serial (finding NFF means the
            unit was serviceable): failure | finding (on-condition defect found at inspection) |
            nff | life | preventive | censored (still fitted at the end of the data).
"""
import numpy as np
import pandas as pd
from sqlalchemy import text

from airpower.model import schema as S


class Fleet:
    def __init__(self, url):
        self.eng = S.engine(url)
        q = lambda sql: pd.read_sql(text(sql), self.eng)          # noqa: E731
        so = q("select tail, takeoff, landing, fh, landings from sortie order by landing")
        so["landing"] = pd.to_datetime(so["landing"])
        self.t0 = pd.to_datetime(so["takeoff"]).min()
        self.t1 = so["landing"].max()
        self.cum = {t: (g["landing"].astype("int64").to_numpy() / 3.6e12, g["fh"].cumsum().to_numpy(),
                        g["landings"].cumsum().to_numpy()) for t, g in so.groupby("tail")}
        self.fleet_fh = float(so["fh"].sum())
        self.fh_by_tail = so.groupby("tail")["fh"].sum()
        inst = q("select tail, position, pn, sn, installed, removed, removal_reason, tso_h_at_export, life_limit from installation")
        for c in ("installed", "removed"):
            inst[c] = pd.to_datetime(inst[c])
        self.inst = inst
        cc = q("select ref, defect_no, tail, at, position, pn, sn_off, sn_on, reason from component_change")
        cc["at"] = pd.to_datetime(cc["at"])
        self.changes = cc
        rr = q("select pn, sn, received, completed, finding from repair_return")
        for c in ("received", "completed"):
            rr[c] = pd.to_datetime(rr[c])
        self.repairs = rr
        de = q("select defect_no, raised from defect")
        de["raised"] = pd.to_datetime(de["raised"])
        raised = dict(zip(de["defect_no"], de["raised"]))
        # a change is logged when the replacement is installed, but the removed unit is handed to stores
        # when the spare is demanded (possibly weeks earlier if the aircraft waited): look for its shop
        # visit from the time the defect was raised
        self.change_raised = {(r.tail, r.sn_off, r.at): raised.get(r.defect_no, r.at) for r in cc.itertuples()}
        self.parts = q("select pn, description, ata, category, unit_cost_inr from part")
        self.aircraft = q("select tail, type_id, unit_id, base from aircraft")

    def usage(self, tail, t0, t1):
        """(flight hours, landings) flown by `tail` between t0 and t1 (arrays)."""
        x, fh, ld = self.cum[tail]
        a = pd.to_datetime(pd.Series(t0)).astype("int64").to_numpy() / 3.6e12
        b = pd.to_datetime(pd.Series(t1)).astype("int64").to_numpy() / 3.6e12
        f = lambda c, v: np.interp(v, x, c, left=0.0)          # noqa: E731
        return f(fh, b) - f(fh, a), f(ld, b) - f(ld, a)

    def repair_finding(self, sn, after):
        """The stores repair-return finding for the first shop visit of `sn` after `after` (defect raised)."""
        r = self.repairs[(self.repairs["sn"] == sn) & (self.repairs["received"] >= after.normalize() - pd.Timedelta(days=1))]
        return r.sort_values("received")["finding"].iloc[0] if len(r) else None

    def intervals(self):
        rows = []
        reason_map = {"UNSCHED": "failure", "INSP": "finding", "LIFE": "life", "PREV": "preventive", "COND": "finding"}
        for r in self.inst.itertuples():
            start = r.installed if pd.notna(r.installed) else self.t0
            end = r.removed if pd.notna(r.removed) else self.t1
            fh, ld = self.usage(r.tail, [start], [end])
            fh, ld = float(fh[0]), float(ld[0])
            if pd.notna(r.installed):
                entry = 0.0
            elif pd.isna(r.removed) and pd.notna(r.tso_h_at_export):
                entry = max(float(r.tso_h_at_export) - fh, 0.0)
            else:
                entry = np.nan
            if pd.isna(r.removed):
                outcome = "censored"
            else:
                outcome = reason_map.get(r.removal_reason, "failure")
                since = self.change_raised.get((r.tail, r.sn, r.removed), r.removed)
                if outcome == "failure" and self.repair_finding(r.sn, since) == "NFF":
                    outcome = "nff"
            rows.append(dict(tail=r.tail, position=r.position, pn=r.pn, sn=r.sn, start=start, end=end,
                             exposure_fh=fh, exposure_landings=ld, entry_fh=entry, exit_fh=entry + fh, outcome=outcome))
        return pd.DataFrame(rows)
