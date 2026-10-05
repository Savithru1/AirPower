"""Write a FleetSim run as four fragmented source systems.

Each system has its own identifiers, formats and habits, as separate legacy systems do:

| System | Files | Aircraft key | Time | Part key |
|---|---|---|---|---|
| HUMS ground station | hums/<base>/<date>.jsonl, one record per download | HUMS unit + airframe MSN (no tail) | UTC, ISO 8601 | serials only |
| Technical records (servicing records) | records/*.csv | "J-1001" | local, dd/mm/yyyy HH:MM | manufacturer P/N |
| Stores (inventory) | stores/*.csv | "1001" (number only), sometimes blank | local date, dd-Mon-yy | stock number (P/N via an incomplete cross-reference) |
| Maintenance agencies | agencies/*.csv, agencies.json | "J 1001" at Base North, "j1001" at Base South | local, yyyy-mm-dd HH:MM | none |

On top of these conventions, record-level faults are injected at the rates in the config
`messiness` block. Every injected fault is written to truth/injections.jsonl, and every
source identifier is mapped to its truth identifier in truth/crosswalk.parquet, so the
integration layer can be scored (tools/reconcile.py). The platform never reads truth/.
"""
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import text

BASE_RECORDS = {"BN": "BASE-N", "BS": "BASE-S", "DP": "DEPOT"}
BASE_STORES = {"BN": "01", "BS": "02", "DP": "90"}
RAISED_BY = {"aircrew": "PILOT", "groundcrew": "GROUND CREW", "inspection": "INSPECTION", "records": "TECH RECORDS",
             "planning": "ENGINEERING"}
CHANGE_REASON = {"failure": "UNSCHED", "nff": "UNSCHED", "inspection": "INSP", "life": "LIFE",
                 "preventive": "PREV", "predictive": "COND", "deferred": "UNSCHED", "snag": "UNSCHED"}
FSC = {72: "2840", 73: "2915", 28: "2915", 49: "2835", 24: "6130", 29: "1650", 32: "1620", 27: "1680", 34: "5826",
       31: "5895", 23: "5821", 21: "4130", 35: "1660", 25: "1377", 26: "6340", 63: "1615", 65: "1615", 62: "1615",
       64: "1615", 67: "1650", 22: "1680"}


class Exporter:
    def __init__(self, sim, out):
        self.sim, self.out = sim, out
        self.cfg = sim.cfg
        self.m = sim.cfg.messiness
        self.rng = np.random.default_rng([sim.seed, 7])
        self.base_dt = datetime.combine(sim.start, datetime.min.time())
        self.utc = timedelta(hours=float(sim.cfg.utc_offset_h))
        self.inj = []
        self.xwalk = []
        self.ac = {a.tail: a for a in sim.aircraft}

    # ------------------------------------------------------------------ helpers
    def local(self, t):
        return self.base_dt + timedelta(hours=float(t))

    def f_records(self, t):
        return None if t is None or pd.isna(t) else self.local(t).strftime("%d/%m/%Y %H:%M")

    def f_agency(self, t):
        return None if t is None or pd.isna(t) else self.local(t).strftime("%Y-%m-%d %H:%M")

    def f_stores(self, t):
        return None if t is None or pd.isna(t) else self.local(t).strftime("%d-%b-%y")

    def f_utc(self, t):
        return (self.local(t) - self.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def tail_records(tail):
        return f"{tail[0]}-{tail[1:]}"

    def tail_agency(self, tail):
        return f"{tail[0]} {tail[1:]}" if self.ac[tail].base == "BN" else tail.lower()

    @staticmethod
    def tail_stores(tail):
        return tail[1:]

    def inject(self, kind, source, key, field=None, clean=None, dirty=None):
        self.inj.append(dict(kind=kind, source=source, key=key, field=field, clean=clean, dirty=dirty))

    def xw(self, entity, truth_id, system, source_id):
        self.xwalk.append(dict(entity=entity, truth_id=truth_id, system=system, source_id=source_id))

    def pn_variant(self, pn, key, field):
        if self.rng.random() >= self.m["pn_format_variant_p"]:
            return pn
        k = int(self.rng.integers(4))
        v = [pn.replace("-", ""), pn.replace("-", " "), pn.lower(), pn + " "][k]
        self.inject("pn_format_variant", "records", key, field, pn, v)
        return v

    def sn_typo(self, sn, key, field):
        if sn is None or self.rng.random() >= self.m["serial_typo_p"]:
            return sn
        k = int(self.rng.integers(4))
        digits = sn[2:]
        if k == 0 and "0" in digits:
            i = 2 + digits.index("0")
            v = sn[:i] + "O" + sn[i + 1:]
        elif k == 1:
            v = sn[:2] + digits.lstrip("0")
        elif k == 2:
            v = sn.lower()
        else:
            v = sn[:-2] + sn[-1] + sn[-2]
        if v == sn:
            return sn
        self.inject("serial_typo", "records", key, field, sn, v)
        return v

    # ------------------------------------------------------------------ parts
    def parts(self):
        sim = self.sim
        self.stock_no, rows = {}, []
        for pn, specs in sim.parts.items():
            s = specs[0]
            h = int(hashlib.md5(pn.encode()).hexdigest(), 16)
            self.stock_no[pn] = f"ST-{FSC.get(s.ata, '9999')}-{h % 1000000:06d}"
        # supersessions: rotables whose P/N changes half-way through the run
        rot = sorted(pn for pn, sp in sim.parts.items() if sp[0].rotable)
        chosen = list(self.rng.choice(rot, size=min(self.m["supersessions"], len(rot)), replace=False))
        t_eff = sim.days * 24.0 * 0.5
        self.superseded = {}
        for pn in chosen:
            head, tail = pn.rsplit("-", 1)
            new = f"{head}-{int(tail) + 1:02d}"
            self.superseded[pn] = (new, t_eff)
            self.inject("pn_superseded", "records", pn, "part_no", pn, new)
        for pn, specs in sim.parts.items():
            s = specs[0]
            xref = pn
            if self.rng.random() < self.m["stores_xref_missing_p"]:
                xref = None
                self.inject("stores_xref_missing", "stores", self.stock_no[pn], "part_no", pn, None)
            rows.append(dict(stock_no=self.stock_no[pn], nomenclature=s.name.upper(), part_no=xref,
                             unit_cost_inr=int(round(s.cost * 1e5)), category="C" if s.consumable else "R",
                             ata=s.ata))
        sup = [dict(old_part_no=o, new_part_no=n, effective=self.f_stores(t)) for o, (n, t) in self.superseded.items()]
        return pd.DataFrame(rows), pd.DataFrame(sup)

    def pn_at(self, pn, t):
        """The P/N the records clerk writes at time t (new number after a supersession)."""
        if pn in self.superseded and t >= self.superseded[pn][1]:
            return self.superseded[pn][0]
        return pn

    # ------------------------------------------------------------------ records
    def records(self, tab):
        sim, L = self.sim, self.sim.log
        ac = pd.DataFrame([dict(tail=self.tail_records(a.tail), msn=a.msn, type=a.type.name, unit=a.unit,
                                base=BASE_RECORDS[a.base], hums_unit=a.hums_unit or "") for a in sim.aircraft])
        # flying log
        seq = Counter()
        self.sortie_ref = {}
        fl = []
        for s in L["sorties"]:
            seq[s["unit"]] += 1
            ref = f"{s['unit']}/F/{seq[s['unit']]:05d}"
            self.sortie_ref[s["id"]] = ref
            self.xw("sortie", s["id"], "records", ref)
            mins = int(round(s["fh"] * 60))
            fl.append(dict(sortie_ref=ref, tail=self.tail_records(s["tail"]), date=self.local(s["t_takeoff"]).strftime("%d/%m/%Y"),
                           takeoff=self.local(s["t_takeoff"]).strftime("%H:%M"), landing=self.local(s["t_landing"]).strftime("%H:%M"),
                           duration=f"{mins // 60}:{mins % 60:02d}", landings=s["landings"], aborted="Y" if s["aborted"] else "N"))
        # work-order numbering per agency (agency system), referenced from records
        self.wo_ref = {}
        wseq = Counter()
        for w in L["work_orders"]:
            wseq[w["agency"]] += 1
            self.wo_ref[w["id"]] = f"{w['agency']}/{wseq[w['agency']]:05d}"
            self.xw("work_order", w["id"], "agencies", self.wo_ref[w["id"]])
        # defects
        dseq = Counter()
        self.defect_no = {}
        rows = []
        for d in L["defects"]:
            yy = self.local(d["t_raised"]).strftime("%y")
            dseq[(d["unit"], yy)] += 1
            no = f"{d['unit']}/{yy}/{dseq[(d['unit'], yy)]:04d}"
            self.defect_no[d["id"]] = no
            self.xw("defect", d["id"], "records", no)
            entered = d["t_raised"]
            if self.rng.random() < self.m["defect_late_entry_p"]:
                lag = float(self.rng.uniform(1, 10)) * 24
                entered = d["t_raised"] + lag
                self.inject("defect_late_entry", "records", no, "entered_on", self.f_records(d["t_raised"]), self.f_records(entered))
            ata = d["ata"]
            if self.rng.random() < self.m["defect_missing_ata_p"]:
                self.inject("defect_missing_ata", "records", no, "ata", ata, None)
                ata = None
            cat = "ADD" if d["deferred"] else ("U/S" if d["grounding"] else "SERVICEABLE")
            row = dict(defect_no=no, tail=self.tail_records(d["tail"]), raised_on=self.f_records(d["t_raised"]),
                       entered_on=self.f_records(entered), raised_by=RAISED_BY[d["raised_by"]], ata=ata,
                       defect=text.noisy(d["text"] or "", self.rng, typo_p=self.m["text_typo_p"]), category=cat,
                       rectification=text.noisy(d["rect"], self.rng, typo_p=self.m["text_typo_p"]) if d["rect"] else None,
                       closed_on=self.f_records(d["t_closed"]), man_hours=round(d["work_h"], 1) if d["t_closed"] else None,
                       wo_ref=self.wo_ref.get(d["wo"]), sortie_ref=self.sortie_ref.get(d["sortie"]))
            rows.append(row)
            if self.rng.random() < self.m["defect_duplicate_p"]:
                dseq[(d["unit"], yy)] += 1
                dup = dict(row)
                dup["defect_no"] = f"{d['unit']}/{yy}/{dseq[(d['unit'], yy)]:04d}"
                dup["raised_on"] = self.f_records(d["t_raised"] + float(self.rng.uniform(0.1, 2.0)))
                dup["entered_on"] = dup["raised_on"]
                dup["defect"] = text.noisy(d["text"] or "", self.rng, typo_p=self.m["text_typo_p"])
                rows.append(dup)
                self.inject("defect_duplicate", "records", dup["defect_no"], "defect_no", no, dup["defect_no"])
        defects = pd.DataFrame(rows)
        defects["ata"] = defects["ata"].astype("Int64")
        # component changes
        rows = []
        for i, c in enumerate(L["changes"]):
            ref = f"CC{i + 1:06d}"
            self.xw("change", f"{c['defect']}:{c['slot']}", "records", ref)
            pn_off = self.pn_variant(self.pn_at(c["pn"], c["t"] - 1e6), ref, "part_no_off")
            pn_on = self.pn_variant(self.pn_at(c["pn"], c["t"]), ref, "part_no_on")
            rows.append(dict(change_ref=ref, defect_no=self.defect_no.get(c["defect"]), tail=self.tail_records(c["tail"]),
                             date=self.f_records(c["t"]), position=c["slot"].replace("#", "-"),
                             part_no_off=pn_off, serial_off=self.sn_typo(c["removed_sn"], ref, "serial_off"),
                             part_no_on=pn_on, serial_on=self.sn_typo(c["installed_sn"], ref, "serial_on"),
                             reason=CHANGE_REASON.get(c["reason"], "UNSCHED")))
        changes = pd.DataFrame(rows)
        # inspections
        insp = pd.DataFrame([dict(insp_ref=i["id"].replace("IN", "INS-"), tail=self.tail_records(i["tail"]),
                                  inspection=i["name"], agency=i["agency"], wo_ref=self.wo_ref.get(i["wo"]),
                                  started=self.f_records(i["t_opened"]), completed=self.f_records(i["t_closed"]),
                                  airframe_hours=round(i["fh"], 1)) for i in L["inspections"]])
        for i in L["inspections"]:
            self.xw("inspection", i["id"], "records", i["id"].replace("IN", "INS-"))
        # life cards: what is fitted at the end of the run
        lc = []
        for a in sim.aircraft:
            for slot, s in a.slots.items():
                if s is None:
                    continue
                lim = s.spec.life
                lc.append(dict(tail=self.tail_records(a.tail), position=slot.replace("#", "-"),
                               part_no=self.pn_at(s.pn, sim.days * 24.0), serial_no=s.sn, tsn_h=round(s.tsn_fh, 1),
                               tso_h=round(s.age_fh, 1), cycles_since_overhaul=int(s.age_cyc),
                               life_limit=";".join(f"{k}={v}" for k, v in lim.items()) or None))
        # inspection status: when each scheduled inspection was last done (the records' forecast basis)
        ist = []
        for a in sim.aircraft:
            for i in a.type.inspections:
                ist.append(dict(tail=self.tail_records(a.tail), inspection=i.name, last_done=self.f_records(a.insp_t[i.id]),
                                last_done_airframe_hours=round(a.insp_fh[i.id], 1), airframe_hours_now=round(a.fh, 1)))
        d = self.out / "records"
        d.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(ist).to_csv(d / "inspection_status.csv", index=False)
        ac.to_csv(d / "aircraft.csv", index=False)
        pd.DataFrame(fl).to_csv(d / "flying_log.csv", index=False)
        defects.to_csv(d / "defects.csv", index=False)
        changes.to_csv(d / "component_changes.csv", index=False)
        insp.to_csv(d / "inspections.csv", index=False)
        pd.DataFrame(lc).to_csv(d / "life_cards.csv", index=False)

    # ------------------------------------------------------------------ agencies
    def agencies(self):
        L = self.sim.log
        d = self.out / "agencies"
        d.mkdir(parents=True, exist_ok=True)
        ag = [dict(agency_id=a["id"], level=a["level"], base=a["base"], capacity=a["capacity"], shift_hours=a["shift_h"],
                   units=a.get("units", [])) for a in self.cfg.agencies]
        (d / "agencies.json").write_text(json.dumps(ag, indent=1))
        rows = []
        for w in L["work_orders"]:
            refs = ";".join(self.defect_no[x] for x in w["defects"] if x in self.defect_no)
            if refs and self.rng.random() < self.m["wo_missing_defect_link_p"]:
                self.inject("wo_missing_defect_link", "agencies", self.wo_ref[w["id"]], "defect_refs", refs, None)
                refs = None
            status = "CLOSED" if w["t_closed"] is not None else ("IN WORK" if w["t_started"] is not None else "OPEN")
            rows.append(dict(wo_id=self.wo_ref[w["id"]], agency_id=w["agency"], aircraft=self.tail_agency(w["tail"]),
                             wo_type=w["kind"].upper(), opened=self.f_agency(w["t_opened"]), started=self.f_agency(w["t_started"]),
                             closed=self.f_agency(w["t_closed"]), defect_refs=refs or None,
                             inspection_ref=w["inspection"].replace("IN", "INS-") if w["inspection"] else None,
                             labour_hours=round(w["labour_h"], 1), status=status))
        pd.DataFrame(rows).to_csv(d / "work_orders.csv", index=False)

    # ------------------------------------------------------------------ stores
    def stores(self, items, sup):
        sim, L = self.sim, self.sim.log
        d = self.out / "stores"
        d.mkdir(parents=True, exist_ok=True)
        items.to_csv(d / "stock_items.csv", index=False)
        sup.to_csv(d / "supersessions.csv", index=False)
        snap = pd.DataFrame(L["stock"])
        start = sim.start
        snap = pd.DataFrame(dict(date=[(start + timedelta(days=int(x))).strftime("%d-%b-%y") for x in snap["day"]],
                                 depot=snap["loc"].map(BASE_STORES), stock_no=snap["pn"].map(self.stock_no),
                                 qty_serviceable=snap["serviceable"], qty_dues_out=snap["backorders"],
                                 qty_in_repair=snap["in_repair"], qty_on_order=snap["on_order"]))
        snap.to_csv(d / "stock_snapshots.csv", index=False)
        self.demand_no = {}
        for i, dm in enumerate(L["demands"]):
            self.demand_no[dm["id"]] = f"DN{i + 1:06d}"
            self.xw("demand", dm["id"], "stores", self.demand_no[dm["id"]])
        rows = []
        for i, dm in enumerate(L["demands"]):
            tail = self.tail_stores(dm["tail"]) if dm["tail"] else None
            if tail and self.rng.random() < self.m["stores_missing_tail_p"]:
                self.inject("stores_missing_tail", "stores", self.demand_no[dm["id"]], "tail", tail, None)
                tail = None
            rows.append(dict(txn_no=f"TX{len(rows) + 1:07d}", date=self.f_stores(dm["t"]), depot=BASE_STORES[dm["loc"]],
                             stock_no=self.stock_no[dm["pn"]], serial_no=None, txn_type="DEMAND",
                             demand_no=self.demand_no[dm["id"]], wo_ref=self.wo_ref.get(dm["wo"]), tail=tail,
                             priority="AOG", qty=1))
        kinds = {"issue": "ISSUE", "receipt": "RECEIPT", "return_us": "RETURN U/S", "transfer_out": "TRANSFER OUT",
                 "scrap": "SCRAP", "condemn": "CONDEMN"}
        for t in L["txns"]:
            rows.append(dict(txn_no=f"TX{len(rows) + 1:07d}", date=self.f_stores(t["t"]), depot=BASE_STORES[t["loc"]],
                             stock_no=self.stock_no[t["pn"]], serial_no=t["sn"], txn_type=kinds[t["kind"]],
                             demand_no=self.demand_no.get(t["demand"]), wo_ref=self.wo_ref.get(t["wo"]),
                             tail=self.tail_stores(t["tail"]) if t["tail"] else None, priority=None, qty=1))
        pd.DataFrame(rows).to_csv(d / "transactions.csv", index=False)
        rr = pd.DataFrame([dict(job_no=f"SJ{i + 1:06d}", stock_no=self.stock_no[r["pn"]], serial_no=r["sn"],
                                received=self.f_stores(r["t_in"]), completed=self.f_stores(r["t_out"]),
                                finding="CONDEMNED" if r["condemned"] else ("NFF" if r["nff"] else "REPAIRED"),
                                cost_inr=int(round(r["cost"] * 1e5))) for i, r in enumerate(L["repairs"])])
        rr.to_csv(d / "repair_returns.csv", index=False)

    # ------------------------------------------------------------------ HUMS
    def hums(self):
        L = self.sim.log
        files = defaultdict(list)
        for h in L["hums"]:
            a = self.ac[h["tail"]]
            dl = h["t_landing"] + float(self.rng.uniform(0.3, 3.0))
            rec = dict(hums_unit=a.hums_unit, airframe_msn=a.msn, download_utc=self.f_utc(dl),
                       flight_start_utc=self.f_utc(h["t_takeoff"]), flight_end_utc=self.f_utc(h["t_landing"]),
                       channels={slot.replace("#", "_"): {k: round(float(v), 5) for k, v in ch.items()}
                                 for slot, ch in h["channels"].items()},
                       serials={slot.replace("#", "_"): sn for slot, sn in h["serials"].items()})
            key = (a.base, self.local(dl).strftime("%Y-%m-%d"))
            files[key].append(rec)
            self.xw("hums", h["sortie"], "hums", f"{a.hums_unit}@{rec['flight_start_utc']}")
            if self.rng.random() < self.m["hums_duplicate_upload_p"]:
                files[key].append(rec)
                self.inject("hums_duplicate", "hums", f"{a.hums_unit}@{rec['flight_start_utc']}")
        for h in L["hums_missing"]:
            self.inject("hums_missing_download", "hums", self.sortie_ref.get(h["sortie"]), "sortie")
        for (base, day), recs in files.items():
            d = self.out / "hums" / base
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{day}.jsonl", "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")

    # ------------------------------------------------------------------ all
    def write(self, tab=None):
        items, sup = self.parts()
        self.records(tab)
        self.agencies()
        self.stores(items, sup)
        self.hums()
        truth = self.out.parent / "truth"
        truth.mkdir(parents=True, exist_ok=True)
        with open(truth / "injections.jsonl", "w") as f:
            for r in self.inj:
                f.write(json.dumps(r, default=str) + "\n")
        pd.DataFrame(self.xwalk).to_parquet(truth / "crosswalk.parquet", index=False)
        conv = dict(tail={"records": "J-1001", "agencies": "J 1001 (Base North) / j1001 (Base South)",
                          "stores": "1001", "hums": "HUMS unit + airframe MSN"},
                    time={"records": "local dd/mm/yyyy HH:MM", "agencies": "local yyyy-mm-dd HH:MM",
                          "stores": "local date dd-Mon-yy", "hums": "UTC ISO 8601"},
                    location={"records": BASE_RECORDS, "stores": BASE_STORES})
        (truth / "conventions.json").write_text(json.dumps(conv, indent=1))
        return dict(Counter(r["kind"] for r in self.inj))


def write(sim, out):
    out.mkdir(parents=True, exist_ok=True)
    return Exporter(sim, out).write()
