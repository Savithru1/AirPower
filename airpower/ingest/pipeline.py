"""Integration pipeline: four fragmented source exports -> one set of canonical tables.

    tables, issues, report = Integrator(source_dir).run()

Steps (each fix or doubt is recorded as a data-quality issue with its resolution):
 1. aircraft master (records) -> tails, MSNs, HUMS units, types, units, bases
 2. agencies, locations
 3. parts: stores items + supersessions -> canonical P/Ns, aliases, stock numbers
 4. life cards, component changes: P/N formats and supersessions, serial typos
 5. stock numbers without a P/N cross-reference: inferred from the serials they moved
 6. stores transactions: P/N via stock number; missing aircraft recovered through the work order
 7. sorties; defects (late entries, duplicates, missing ATA inferred), work orders and their links
 8. HUMS downloads: aircraft via HUMS unit / MSN, duplicate uploads merged, linked to sorties
 9. installation history rebuilt from changes, checked against life cards
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from rapidfuzz import fuzz

from .resolve import PartResolver, SerialResolver, TailResolver, key, majority, norm_serial, normalize_text

REGISTRY = Path(__file__).with_name("registry.yaml")


ACRONYMS = {"AFCS", "GPS", "INS", "IFF", "V/UHF", "MFD", "JFS", "HUMS", "ECS", "TRU", "OBOGS"}


def _nomenclature(s):
    """Stores nomenclature (upper case) -> readable, keeping acronyms."""
    return " ".join(w if w in ACRONYMS else w.capitalize() for w in str(s).split())


def _nz(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT else v


class Integrator:
    def __init__(self, source_dir, registry=REGISTRY):
        self.src = Path(source_dir)
        self.reg = yaml.safe_load(Path(registry).read_text())
        self.issues = []
        self.conversions = Counter()
        self.counts = defaultdict(dict)
        self.t = {}

    # ------------------------------------------------------------------ helpers
    def issue(self, source, entity, record_key, rule, severity, resolution, field=None, raw=None, resolved=None,
              detail=None):
        self.issues.append(dict(source=source, entity=entity, record_key=None if record_key is None else str(record_key),
                                rule=rule, severity=severity, field=field,
                                raw_value=None if raw is None else str(raw),
                                resolved_value=None if resolved is None else str(resolved),
                                resolution=resolution, detail=detail))

    def to_utc(self, system, s, fmt_key="time_format"):
        r = self.reg[system]
        dt = pd.to_datetime(s, format=r[fmt_key], errors="coerce")
        return dt - pd.Timedelta(hours=r["utc_offset_h"])

    def read_csv(self, rel):
        return pd.read_csv(self.src / rel, dtype=str, keep_default_na=False, na_values=[""])

    # ------------------------------------------------------------------ 1-2 master data
    def master(self):
        ac = self.read_csv("records/aircraft.csv")
        loc = self.reg["records"]["locations"]
        ac["tail_c"] = ac["tail"].map(key)
        self.conversions["records.tail"] += int((ac["tail"] != ac["tail_c"]).sum())
        self.tails = TailResolver(ac["tail_c"])
        self.t["aircraft_type"] = (ac.assign(id=ac["tail_c"].str[0])[["id", "type"]].drop_duplicates("id")
                                   .rename(columns={"type": "name"}))
        self.t["aircraft"] = pd.DataFrame(dict(tail=ac["tail_c"], msn=ac["msn"], type_id=ac["tail_c"].str[0],
                                               unit_id=ac["unit"], base=ac["base"].map(loc), hums_unit=ac["hums_unit"]))
        self.t["unit"] = self.t["aircraft"][["unit_id", "type_id", "base"]].drop_duplicates("unit_id").rename(columns={"unit_id": "id"})
        self.hums_map = {r.hums_unit: (r.tail, r.msn) for r in self.t["aircraft"].itertuples() if isinstance(r.hums_unit, str)}
        ag = json.loads((self.src / "agencies" / "agencies.json").read_text())
        self.t["agency"] = pd.DataFrame([dict(id=a["agency_id"], level=a["level"], base=a["base"], capacity=a["capacity"],
                                              shift_hours=a["shift_hours"]) for a in ag])
        names = {"BN": "Base North", "BS": "Base South", "DP": "Central Depot"}
        codes = sorted(set(self.t["agency"]["base"]) | set(self.t["aircraft"]["base"]))
        self.t["location"] = pd.DataFrame([dict(code=c, name=names.get(c, c), kind="depot" if c == "DP" else "base")
                                           for c in codes])
        self.counts["records"]["aircraft"] = len(ac)

    # ------------------------------------------------------------------ 3 parts
    def parts(self):
        items = self.read_csv("stores/stock_items.csv")
        sup = self.read_csv("stores/supersessions.csv")
        self.items, self.sup = items, sup
        self.pr = PartResolver(items, sup)
        for r in sup.itertuples():
            self.issue("stores", "part", r.old_part_no, "superseded_part_number", "info", "linked", "part_no",
                       r.old_part_no, r.new_part_no, f"effective {r.effective}")
        self.counts["stores"]["stock_items"] = len(items)

    def _part_table(self):
        rows, aliases = [], []
        for r in self.items.itertuples():
            pn = self.pr.from_stock(r.stock_no)
            if pn is None:
                continue
            rows.append(dict(pn=pn, description=_nomenclature(r.nomenclature), ata=int(r.ata), category=r.category,
                             unit_cost_inr=int(r.unit_cost_inr), stock_no=r.stock_no))
        parts = pd.DataFrame(rows).drop_duplicates("pn")
        for k, (c, kind) in self.pr.alias.items():
            aliases.append(dict(alias=k, pn=self.pr.name(c), kind=kind, source="stores"))
        for s, c in self.pr.stock.items():
            aliases.append(dict(alias=key(s), pn=self.pr.name(c), kind="stock_no", source="stores"))
        self.t["part"] = parts
        self.t["part_alias"] = pd.DataFrame(aliases).drop_duplicates("alias")

    # ------------------------------------------------------------------ 4 records: life cards, changes
    def read_components(self):
        lc = self.read_csv("records/life_cards.csv")
        lc["tail"] = lc["tail"].map(key)
        lc["sn"] = lc["serial_no"].map(norm_serial)
        self.lc = lc
        ch = self.read_csv("records/component_changes.csv")
        ch["tail_c"] = ch["tail"].map(key)
        ch["at"] = self.to_utc("records", ch["date"])
        self.ch = ch
        self.counts["records"]["component_changes"] = len(ch)
        self.counts["records"]["life_cards"] = len(lc)

    def resolve_components(self):
        self.lc["pn"] = [self.pr(p)[0] for p in self.lc["part_no"]]
        pns = []
        for r in self.ch.itertuples():
            pn, why = self.pr(r.part_no_on)
            pn_off, why_off = self.pr(r.part_no_off)
            for raw, w, f in ((r.part_no_on, why, "part_no_on"), (r.part_no_off, why_off, "part_no_off")):
                if w == "format":
                    self.issue("records", "component_change", r.change_ref, "part_number_format", "info", "fixed", f,
                               raw, pn or pn_off)
                elif w == "superseded":
                    self.conversions["records.superseded_pn"] += 1
                elif w in ("unknown", "missing"):
                    self.issue("records", "component_change", r.change_ref, "unknown_part_number", "error", "flagged",
                               f, raw)
            pns.append(pn or pn_off)
        self.ch["pn"] = pns

    # ------------------------------------------------------------------ 5-6 stores
    def stores(self):
        tx = self.read_csv("stores/transactions.csv")
        loc = self.reg["stores"]["locations"]
        tx["date_c"] = self.to_utc("stores", tx["date"], "date_format")
        tx["location"] = tx["depot"].map(loc)
        # 5. stock numbers without cross-reference: vote with the serials they moved, using the
        #    P/Ns the records system wrote against those serials (raw, before resolution)
        sn_pn, printed = {}, defaultdict(Counter)
        pairs = list(zip(self.lc["part_no"], self.lc["serial_no"])) + \
            list(zip(self.ch["part_no_on"], self.ch["serial_on"])) + list(zip(self.ch["part_no_off"], self.ch["serial_off"]))
        for raw_pn, raw_sn in pairs:
            sn = norm_serial(raw_sn)
            if sn and isinstance(raw_pn, str):
                sn_pn.setdefault(sn, self.pr.current(key(raw_pn)))      # follow supersessions before voting
                printed[key(raw_pn)][raw_pn.strip()] += 1
        for stock_no in list(self.pr.unmapped_stock):
            serials = tx.loc[(tx["stock_no"] == stock_no) & tx["serial_no"].notna(), "serial_no"].unique()
            pk, n, total = majority(sn_pn.get(norm_serial(s)) for s in serials)
            if pk and (n == total or n >= max(2, 0.6 * total)):        # unanimous, or a clear majority
                forms = printed.get(pk)
                pn = max(forms, key=lambda f: (f == f.upper() and "-" in f, forms[f])) if forms else self.pr.name(pk)
                self.pr.display.setdefault(pk, pn)
                info = self.pr.infer_stock(stock_no, pn, n, total)
                self.issue("stores", "part", stock_no, "stock_xref_missing", "warning", "linked", "part_no", None,
                           info["pn"], f"inferred from {n} of {total} serials moved under this stock number")
            else:
                self.issue("stores", "part", stock_no, "stock_xref_missing", "error", "flagged", "part_no",
                           detail=f"{len(serials)} serials, no majority P/N")
        self._part_table()
        self.resolve_components()
        tx["pn"] = tx["stock_no"].map(self.pr.from_stock)
        # serial universe from clean systems (stores movements, life cards) for record serial checks
        self.sr = SerialResolver()
        for r in tx.itertuples():
            if isinstance(r.serial_no, str):
                self.sr.add(r.pn, r.serial_no.strip().upper())
        for r in self.lc.itertuples():
            self.sr.add(r.pn, r.sn)
        self.tx = tx
        self.counts["stores"]["transactions"] = len(tx)

    # ------------------------------------------------------------------ 7 sorties, defects, work orders
    def flying(self):
        fl = self.read_csv("records/flying_log.csv")
        fmt = self.reg["records"]["time_format"]
        off = pd.Timedelta(hours=self.reg["records"]["utc_offset_h"])
        to = pd.to_datetime(fl["date"] + " " + fl["takeoff"], format=fmt) - off
        la = pd.to_datetime(fl["date"] + " " + fl["landing"], format=fmt) - off
        la = la.where(la >= to, la + pd.Timedelta(days=1))
        hm = fl["duration"].str.split(":", expand=True).astype(int)
        self.t["sortie"] = pd.DataFrame(dict(ref=fl["sortie_ref"], tail=fl["tail"].map(key), takeoff=to, landing=la,
                                             fh=hm[0] + hm[1] / 60.0, landings=fl["landings"].astype(int),
                                             aborted=fl["aborted"] == "Y"))
        self.counts["records"]["sorties"] = len(fl)

    def defects(self):
        lk = self.reg["linking"]
        de = self.read_csv("records/defects.csv")
        de["tail_c"] = de["tail"].map(key)
        de["raised"] = self.to_utc("records", de["raised_on"])
        de["entered"] = self.to_utc("records", de["entered_on"])
        de["closed"] = self.to_utc("records", de["closed_on"])
        de["text_norm"] = de["defect"].map(normalize_text)
        de["ata"] = pd.to_numeric(de["ata"], errors="coerce")
        de["ata_source"] = np.where(de["ata"].notna(), "record", None)
        # timeliness
        lag_h = (de["entered"] - de["raised"]).dt.total_seconds() / 3600
        for r in de[lag_h > lk["late_entry_h"]].itertuples():
            self.issue("records", "defect", r.defect_no, "late_entry", "info", "flagged", "entered_on", r.entered_on,
                       detail=f"entered {(r.entered - r.raised).total_seconds() / 86400:.1f} days after the event")
        # duplicates: same aircraft, similar text, raised within the window, later entry is the copy
        de = de.sort_values(["tail_c", "raised"]).reset_index(drop=True)
        dup_of = [None] * len(de)
        win = pd.Timedelta(hours=lk["duplicate_defect_window_h"])
        has_change = set(self.ch["defect_no"].dropna())

        def same_wo(i, j):
            a, b = de.at[i, "wo_ref"], de.at[j, "wo_ref"]
            return (pd.isna(a) and pd.isna(b)) or a == b

        for _, g in de.groupby("tail_c"):
            idx = list(g.index)
            for a_i, i in enumerate(idx):
                if dup_of[i] is not None:
                    continue
                for j in idx[a_i + 1:]:
                    if de.at[j, "raised"] - de.at[i, "raised"] > win:
                        break
                    # a copy has no part change of its own and carries the original's work order
                    if dup_of[j] is None and de.at[j, "category"] == de.at[i, "category"] and \
                            de.at[j, "defect_no"] not in has_change and same_wo(i, j) and \
                            fuzz.token_set_ratio(de.at[i, "text_norm"], de.at[j, "text_norm"]) >= lk["duplicate_text_similarity"]:
                        dup_of[j] = de.at[i, "defect_no"]
                        self.issue("records", "defect", de.at[j, "defect_no"], "duplicate_record", "warning", "merged",
                                   "defect_no", de.at[j, "defect_no"], de.at[i, "defect_no"])
        de["duplicate_of"] = dup_of
        # missing ATA: from the part changed under the defect, else from the text
        part_ata = self.t["part"].set_index("pn")["ata"].to_dict()
        ch_ata = {r.defect_no: part_ata.get(r.pn) for r in self.ch.itertuples() if isinstance(r.defect_no, str)}
        miss = de["ata"].isna()
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        lab = de[~miss]
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2)
        X = vec.fit_transform(lab["text_norm"])
        clf = LogisticRegression(max_iter=2000, C=5.0).fit(X, lab["ata"].astype(int))
        for i in de.index[miss]:
            no = de.at[i, "defect_no"]
            if ch_ata.get(no):
                de.at[i, "ata"], de.at[i, "ata_source"] = ch_ata[no], "inferred"
                self.issue("records", "defect", no, "missing_ata", "warning", "fixed", "ata", None, ch_ata[no],
                           "from the part replaced under this defect")
                continue
            p = clf.predict_proba(vec.transform([de.at[i, "text_norm"]]))[0]
            k = int(np.argmax(p))
            if p[k] >= 0.5:
                de.at[i, "ata"], de.at[i, "ata_source"] = int(clf.classes_[k]), "inferred"
                self.issue("records", "defect", no, "missing_ata", "warning", "fixed", "ata", None, int(clf.classes_[k]),
                           f"from the defect text (p = {p[k]:.2f})")
            else:
                self.issue("records", "defect", no, "missing_ata", "warning", "flagged", "ata",
                           detail=f"text classifier not confident (best p = {p[k]:.2f})")
        self.de = de
        self.counts["records"]["defects"] = len(de)

    def work_orders(self):
        wo = self.read_csv("agencies/work_orders.csv")
        wo["tail_c"] = [self.tails(x)[0] for x in wo["aircraft"]]
        self.conversions["agencies.tail"] += int((wo["aircraft"] != wo["tail_c"]).sum())
        for c in ("opened", "started", "closed"):
            wo[c + "_c"] = self.to_utc("agencies", wo[c])
        links = {}
        for r in wo.itertuples():
            if isinstance(r.defect_refs, str):
                for d in r.defect_refs.split(";"):
                    links[(r.wo_id, d)] = "explicit"
        wo_ids = set(wo["wo_id"])
        # scheduled (inspection) work orders never list defects in the agency system: their findings
        # are linked from the records side without it being a data error
        wo_has_refs = set(wo.loc[wo["defect_refs"].notna() | (wo["wo_type"] != "UNSCHEDULED"), "wo_id"])
        for r in self.de.itertuples():
            if not isinstance(r.wo_ref, str):
                continue
            if r.wo_ref not in wo_ids:
                self.issue("records", "defect", r.defect_no, "unknown_work_order", "error", "flagged", "wo_ref", r.wo_ref)
                continue
            if (r.wo_ref, r.defect_no) not in links and pd.isna(r.duplicate_of):
                links[(r.wo_ref, r.defect_no)] = "record"
                if r.wo_ref not in wo_has_refs:
                    self.issue("agencies", "work_order", r.wo_ref, "missing_defect_link", "warning", "linked",
                               "defect_refs", None, r.defect_no, "recovered from the defect's work-order reference")
        self.t["work_order_defect"] = pd.DataFrame([dict(wo_id=w, defect_no=d, link=l) for (w, d), l in links.items()])
        self.t["work_order"] = pd.DataFrame(dict(wo_id=wo["wo_id"], agency_id=wo["agency_id"], tail=wo["tail_c"],
                                                 wo_type=wo["wo_type"], opened=wo["opened_c"], started=wo["started_c"],
                                                 closed=wo["closed_c"], inspection_ref=wo["inspection_ref"],
                                                 labour_hours=pd.to_numeric(wo["labour_hours"]), status=wo["status"]))
        self.wo_tail = dict(zip(wo["wo_id"], wo["tail_c"]))
        self.counts["agencies"]["work_orders"] = len(wo)

    def finish_records(self):
        de = self.de
        self.t["defect"] = pd.DataFrame(dict(
            defect_no=de["defect_no"], tail=de["tail_c"], raised=de["raised"], entered=de["entered"],
            raised_by=de["raised_by"], ata=de["ata"].astype("Int64"), ata_source=de["ata_source"], text=de["defect"],
            text_norm=de["text_norm"], category=de["category"], rectification=de["rectification"], closed=de["closed"],
            man_hours=pd.to_numeric(de["man_hours"]), wo_id=de["wo_ref"], sortie_ref=de["sortie_ref"],
            duplicate_of=de["duplicate_of"]))
        ch = self.ch
        # context: the serials stores issued / received back against the same work order and part
        from rapidfuzz.distance import DamerauLevenshtein
        ctx = defaultdict(set)
        for r in self.tx.itertuples():
            if isinstance(r.wo_ref, str) and isinstance(r.serial_no, str) and r.txn_type in ("ISSUE", "RETURN U/S"):
                ctx[(r.wo_ref, r.pn, r.txn_type)].add(r.serial_no.strip().upper())
        de_wo = dict(zip(self.de["defect_no"], self.de["wo_ref"]))
        sn_off, sn_on = [], []
        for r in ch.itertuples():
            wo = de_wo.get(r.defect_no)
            for raw, f, out in ((r.serial_off, "serial_off", sn_off), (r.serial_on, "serial_on", sn_on)):
                s, why = self.sr(r.pn, raw)
                seen = ctx.get((wo, r.pn, "ISSUE" if f == "serial_on" else "RETURN U/S"), set())
                if s and seen and s not in seen:
                    near = [x for x in seen if DamerauLevenshtein.distance(s, x) <= 1]
                    if len(near) == 1:
                        s, why = near[0], "context"
                out.append(s)
                if why == "context":
                    self.issue("records", "component_change", r.change_ref, "serial_number_error", "warning", "fixed",
                               f, raw, s, "valid but wrong serial: stores moved a different one on this work order")
                    continue
                if why in ("normalised", "fuzzy"):
                    self.issue("records", "component_change", r.change_ref, "serial_number_error", "warning", "fixed",
                               f, raw, s, "transposition / typo matched to a known serial" if why == "fuzzy" else
                               "case, O/0, I/1 or dropped leading zeros")
                elif why == "unknown":
                    self.issue("records", "component_change", r.change_ref, "unknown_serial", "error", "flagged", f, raw)
        self.t["component_change"] = pd.DataFrame(dict(
            ref=ch["change_ref"], defect_no=ch["defect_no"], tail=ch["tail_c"], at=ch["at"], position=ch["position"],
            pn=ch["pn"], sn_off=sn_off, sn_on=sn_on, reason=ch["reason"], raw_pn_off=ch["part_no_off"],
            raw_pn_on=ch["part_no_on"], raw_sn_off=ch["serial_off"], raw_sn_on=ch["serial_on"]))
        ins = self.read_csv("records/inspections.csv")
        self.t["inspection"] = pd.DataFrame(dict(
            ref=ins["insp_ref"], tail=ins["tail"].map(key), name=ins["inspection"], agency_id=ins["agency"],
            wo_id=ins["wo_ref"], started=self.to_utc("records", ins["started"]),
            completed=self.to_utc("records", ins["completed"]), airframe_hours=pd.to_numeric(ins["airframe_hours"])))
        # installation history per (tail, position), checked against the life cards
        inst = []
        cc = self.t["component_change"].sort_values("at")
        for (tail, pos), g in cc.groupby(["tail", "position"]):
            prev_t = None
            for r in g.itertuples():
                inst.append(dict(tail=tail, position=pos, pn=r.pn, sn=r.sn_off, installed=prev_t, removed=r.at,
                                 removal_reason=r.reason))
                prev_t = r.at
            last = g.iloc[-1]
            inst.append(dict(tail=tail, position=pos, pn=last.pn, sn=last.sn_on, installed=prev_t, removed=None,
                             removal_reason=None))
        tso = {(r.tail, r.position): float(r.tso_h) for r in self.lc.itertuples() if pd.notna(r.tso_h)}
        lim = {(r.tail, r.position): r.life_limit for r in self.lc.itertuples() if isinstance(r.life_limit, str)}
        lim_pn = {}
        for r in self.lc.itertuples():
            if isinstance(r.life_limit, str) and r.pn:
                lim_pn[r.pn] = r.life_limit                      # life limits are a property of the part number
        for i in inst:
            i["life_limit"] = lim.get((i["tail"], i["position"])) or lim_pn.get(i["pn"])
            if i["removed"] is None:
                i["tso_h_at_export"] = tso.get((i["tail"], i["position"]))
        changed = {(i["tail"], i["position"]) for i in inst}
        current = {(i["tail"], i["position"]): i["sn"] for i in inst if i["removed"] is None}
        for r in self.lc.itertuples():
            k = (r.tail, r.position)
            if k not in changed:
                inst.append(dict(tail=r.tail, position=r.position, pn=r.pn, sn=r.sn, installed=None, removed=None,
                                 removal_reason=None, tso_h_at_export=tso.get(k), life_limit=lim.get(k)))
            elif current.get(k) != r.sn:
                self.issue("records", "installation", f"{r.tail}/{r.position}", "life_card_mismatch", "error",
                           "flagged", "serial_no", current.get(k), r.sn, "last recorded change does not match life card")
        self.t["installation"] = pd.DataFrame(inst)
        st_path = self.src / "records" / "inspection_status.csv"
        if st_path.exists():
            st = self.read_csv("records/inspection_status.csv")
            self.t["inspection_status"] = pd.DataFrame(dict(
                tail=st["tail"].map(key), name=st["inspection"], last_done=self.to_utc("records", st["last_done"]),
                last_done_fh=pd.to_numeric(st["last_done_airframe_hours"]),
                airframe_fh_at_export=pd.to_numeric(st["airframe_hours_now"])))

    def finish_stores(self):
        tx = self.tx
        tails, srcs = [], []
        for r in tx.itertuples():
            if isinstance(r.tail, str):
                t, _ = self.tails(r.tail)
                tails.append(t)
                srcs.append("record")
            elif isinstance(r.wo_ref, str) and r.wo_ref in self.wo_tail:
                tails.append(self.wo_tail[r.wo_ref])
                srcs.append("via_wo")
                if r.txn_type == "DEMAND":
                    self.issue("stores", "stock_txn", r.demand_no, "missing_aircraft", "warning", "linked", "tail",
                               None, self.wo_tail[r.wo_ref], "recovered through the work order")
            else:
                tails.append(None)
                srcs.append(None)
        self.conversions["stores.tail"] += int(tx["tail"].notna().sum())
        self.t["stock_txn"] = pd.DataFrame(dict(
            txn_no=tx["txn_no"], date=tx["date_c"], location=tx["location"], pn=tx["pn"],
            sn=tx["serial_no"].str.strip().str.upper(), txn_type=tx["txn_type"], demand_no=tx["demand_no"],
            wo_id=tx["wo_ref"], tail=tails, tail_source=srcs, priority=tx["priority"]))
        snap = self.read_csv("stores/stock_snapshots.csv")
        self.t["stock_snapshot"] = pd.DataFrame(dict(
            date=self.to_utc("stores", snap["date"], "date_format"), location=snap["depot"].map(self.reg["stores"]["locations"]),
            pn=snap["stock_no"].map(self.pr.from_stock), serviceable=snap["qty_serviceable"].astype(int),
            dues_out=snap["qty_dues_out"].astype(int), in_repair=snap["qty_in_repair"].astype(int),
            on_order=snap["qty_on_order"].astype(int))).dropna(subset=["pn"])
        rr = self.read_csv("stores/repair_returns.csv")
        self.t["repair_return"] = pd.DataFrame(dict(
            job_no=rr["job_no"], pn=rr["stock_no"].map(self.pr.from_stock), sn=rr["serial_no"],
            received=self.to_utc("stores", rr["received"], "date_format"),
            completed=self.to_utc("stores", rr["completed"], "date_format"), finding=rr["finding"],
            cost_inr=rr["cost_inr"].astype(int)))

    # ------------------------------------------------------------------ 8 HUMS
    def hums(self):
        tol = pd.Timedelta(minutes=self.reg["linking"]["hums_sortie_tolerance_min"])
        recs, seen = [], {}
        for f in sorted((self.src / "hums").glob("*/*.jsonl")):
            for line in f.read_text().splitlines():
                h = json.loads(line)
                k = (h["hums_unit"], h["flight_start_utc"])
                if k in seen:
                    self.issue("hums", "hums_record", f"{k[0]}@{k[1]}", "duplicate_upload", "info", "merged",
                               detail=f"also in {seen[k]}")
                    continue
                seen[k] = f.name
                tail, msn = self.hums_map.get(h["hums_unit"], (None, None))
                if tail is None or msn != h["airframe_msn"]:
                    self.issue("hums", "hums_record", f"{k[0]}@{k[1]}", "unknown_hums_unit", "error", "dropped",
                               "hums_unit", h["hums_unit"])
                    continue
                h["tail"], h["file"] = tail, f"{f.parent.name}/{f.name}"
                recs.append(h)
        fmt = self.reg["hums"]["time_format"]
        hr = pd.DataFrame(dict(tail=[h["tail"] for h in recs], hums_unit=[h["hums_unit"] for h in recs],
                               flight_start=pd.to_datetime([h["flight_start_utc"] for h in recs], format=fmt),
                               flight_end=pd.to_datetime([h["flight_end_utc"] for h in recs], format=fmt),
                               downloaded=pd.to_datetime([h["download_utc"] for h in recs], format=fmt),
                               source_file=[h["file"] for h in recs]))
        hr["id"] = np.arange(1, len(hr) + 1)
        so = self.t["sortie"][["ref", "tail", "takeoff"]].sort_values("takeoff")
        m = pd.merge_asof(hr.sort_values("flight_start"), so, left_on="flight_start", right_on="takeoff", by="tail",
                          direction="nearest", tolerance=tol)
        hr = hr.merge(m[["id", "ref"]], on="id", how="left").rename(columns={"ref": "sortie_ref"})
        for r in hr[hr["sortie_ref"].isna()].itertuples():
            self.issue("hums", "hums_record", f"{r.hums_unit}@{r.flight_start}", "no_matching_sortie", "warning", "flagged")
        fitted = set(self.t["aircraft"].loc[self.t["aircraft"]["hums_unit"].notna(), "tail"])
        got = set(hr["sortie_ref"].dropna())
        for r in self.t["sortie"].itertuples():
            if r.tail in fitted and r.ref not in got:
                self.issue("hums", "sortie", r.ref, "missing_hums_download", "warning", "flagged",
                           detail="HUMS-fitted aircraft flew but no download was received")
        rows = []
        idmap = dict(zip(zip(hr["hums_unit"], hr["flight_start"].dt.strftime(fmt)), hr["id"]))
        for h in recs:
            rid = idmap[(h["hums_unit"], h["flight_start_utc"])]
            ts = pd.Timestamp(h["flight_end_utc"]).tz_localize(None)
            for pos, ch in h["channels"].items():
                for name, v in ch.items():
                    rows.append((ts, rid, h["tail"], pos, h["serials"].get(pos), name, v))
        hi = pd.DataFrame(rows, columns=["ts", "hums_record_id", "tail", "position", "sn", "channel", "value"])
        hi.insert(1, "id", np.arange(1, len(hi) + 1))
        self.t["hums_record"] = hr[["id", "tail", "hums_unit", "sortie_ref", "flight_start", "flight_end", "downloaded",
                                    "source_file"]]
        self.t["health_indicator"] = hi
        self.counts["hums"]["downloads"] = len(seen) + sum(1 for i in self.issues if i["rule"] == "duplicate_upload")
        self.counts["hums"]["records"] = len(hr)

    # ------------------------------------------------------------------ run
    def run(self):
        self.master()
        self.parts()
        self.read_components()
        self.stores()
        self.flying()
        self.defects()
        self.work_orders()
        self.finish_records()
        self.finish_stores()
        self.hums()
        issues = pd.DataFrame(self.issues)
        report = dict(
            source_dir=str(self.src), records_in={k: dict(v) for k, v in self.counts.items()},
            convention_conversions=dict(self.conversions),
            issues_by_rule=issues.groupby(["source", "rule", "resolution"]).size().reset_index(name="n").to_dict("records")
            if len(issues) else [],
            tables={k: int(len(v)) for k, v in self.t.items()})
        return self.t, issues, report
