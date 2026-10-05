"""Aircraft serviceability derived from the integrated records (no simulator truth).

Evidence per aircraft:
* SCHEDULED  - an inspection is open (started .. completed);
* NMCM       - an unscheduled work order is open, or a U/S defect is open;
* NMCS       - inside one of the above, a stores demand raised against this aircraft is not
               yet issued (stores dates are day-resolution, so NMCS is approximate to a day).
Priority: NMCS > SCHEDULED > NMCM > SERVICEABLE (an aircraft held for a part is reported as
waiting for supply even when the part was found at an inspection, as the truth model does).
"""
import numpy as np
import pandas as pd

PRIORITY = {"NMCS": 3, "SCHEDULED": 2, "NMCM": 1}


def _intervals(t):
    """Evidence intervals [tail, start, end, status, reason] from canonical tables."""
    end_all = max(t["sortie"]["landing"].max(), t["work_order"]["closed"].max()) + pd.Timedelta(days=1)
    rows = []
    for r in t["inspection"].itertuples():
        rows.append((r.tail, r.started, r.completed if pd.notna(r.completed) else end_all, "SCHEDULED", r.name))
    wo = t["work_order"]
    for r in wo[wo["wo_type"] == "UNSCHEDULED"].itertuples():
        rows.append((r.tail, r.opened, r.closed if pd.notna(r.closed) else end_all, "NMCM", r.wo_id))
    de = t["defect"]
    de = de[(de["category"] == "U/S") & de["duplicate_of"].isna()]
    for r in de.itertuples():
        rows.append((r.tail, r.raised, r.closed if pd.notna(r.closed) else end_all, "NMCM", r.defect_no))
    tx = t["stock_txn"]
    dm = tx[tx["txn_type"] == "DEMAND"][["demand_no", "date", "tail", "pn"]]
    iss = tx[tx["txn_type"] == "ISSUE"].groupby("demand_no")["date"].min()
    dm = dm.assign(issued=dm["demand_no"].map(iss))
    dm = dm[dm["tail"].notna() & ((dm["issued"].isna()) | (dm["issued"] > dm["date"]))]
    for r in dm.itertuples():
        rows.append((r.tail, r.date, r.issued if pd.notna(r.issued) else end_all, "NMCS", f"{r.demand_no} {r.pn}"))
    ev = pd.DataFrame(rows, columns=["tail", "start", "end", "status", "reason"])
    return ev[ev["end"] > ev["start"]], end_all


def derive(t):
    """-> (intervals DataFrame [tail, start, end, status, reason], window start, window end)."""
    ev, end_all = _intervals(t)
    t0 = t["sortie"]["takeoff"].min().normalize()
    out = []
    for tail in t["aircraft"]["tail"]:
        e = ev[ev["tail"] == tail]
        cuts = sorted(set([t0, end_all]) | set(e["start"]) | set(e["end"]))
        cuts = [c for c in cuts if t0 <= c <= end_all]
        st = e["start"].to_numpy()
        en = e["end"].to_numpy()
        pr = e["status"].map(PRIORITY).to_numpy()
        why = e["reason"].to_numpy()
        prev = None
        for a, b in zip(cuts[:-1], cuts[1:]):
            mid = a + (b - a) / 2
            on = (st <= mid) & (en > mid)
            if on.any():
                act = pr[on]
                k = act.max()
                status = {3: "NMCS", 2: "SCHEDULED", 1: "NMCM"}[k]
                reason = "; ".join(sorted(set(why[on][act == k])))[:500]
            else:
                status, reason = "SERVICEABLE", None
            if prev and prev[3] == status and prev[2] == a:
                prev[2] = b
            else:
                prev = [tail, a, b, status, reason]
                out.append(prev)
    iv = pd.DataFrame(out, columns=["tail", "start", "end", "status", "reason"])
    return iv, t0, end_all


def daily(iv, aircraft, at_local_hour=8.0, utc_offset_h=5.5):
    """Status of every aircraft at a local time of day, per day -> counts per unit and day."""
    start = iv["start"].min().normalize()
    end = iv["end"].max().normalize()
    days = pd.date_range(start, end, freq="D")
    when = days + pd.Timedelta(hours=at_local_hour - utc_offset_h)
    unit = aircraft.set_index("tail")["unit_id"]
    rows = []
    for tail, g in iv.groupby("tail"):
        g = g.sort_values("start")
        idx = np.searchsorted(g["start"].to_numpy(), when.to_numpy(), side="right") - 1
        ok = idx >= 0
        st = np.where(ok, g["status"].to_numpy()[np.clip(idx, 0, None)], None)
        rows.append(pd.DataFrame(dict(day=days, tail=tail, unit=unit[tail], status=st)))
    d = pd.concat(rows)
    c = d.pivot_table(index=["day", "unit"], columns="status", values="tail", aggfunc="count", fill_value=0)
    c = c.reindex(columns=["SERVICEABLE", "NMCM", "NMCS", "SCHEDULED"], fill_value=0).reset_index()
    c["aircraft"] = c[["SERVICEABLE", "NMCM", "NMCS", "SCHEDULED"]].sum(axis=1)
    return c
