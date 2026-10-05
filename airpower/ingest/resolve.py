"""Entity resolution: one identity for each aircraft, part and serial across the four systems.

Every resolver returns the canonical value and a reason when it had to change or infer
something; the caller records that reason as a data-quality issue.
"""
import re
from collections import Counter, defaultdict

from rapidfuzz.distance import DamerauLevenshtein

_ALNUM = re.compile(r"[^A-Za-z0-9]")


def key(raw):
    return _ALNUM.sub("", str(raw)).upper() if raw is not None else ""


# ------------------------------------------------------------------ aircraft
class TailResolver:
    """Canonical tail = type letter + number ("J1001"). Accepts "J-1001", "J 1001", "j1001", "1001"."""

    def __init__(self, tails):
        self.tails = set(tails)
        self.by_number = defaultdict(list)
        for t in self.tails:
            self.by_number[t[1:]].append(t)

    def __call__(self, raw):
        if raw is None or (isinstance(raw, float) and raw != raw) or str(raw).strip() == "":
            return None, "missing"
        k = key(raw)
        if k in self.tails:
            return k, None if str(raw) == k else "format"
        if k.isdigit() and len(self.by_number.get(k, [])) == 1:
            return self.by_number[k][0], "number_only"
        return None, "unknown"


# ------------------------------------------------------------------ parts
class PartResolver:
    """Canonical P/N = the current part number after following supersessions.
    Aliases: every P/N in any format (key without separators, case-insensitive), superseded
    numbers, and stock numbers (from the stores cross-reference, or inferred)."""

    def __init__(self, items, supersessions):
        self.succ = {key(r.old_part_no): key(r.new_part_no) for r in supersessions.itertuples()}
        self.display = {}                       # key -> printed form
        for r in supersessions.itertuples():
            self.display[key(r.old_part_no)] = r.old_part_no
            self.display[key(r.new_part_no)] = r.new_part_no
        self.alias = {}                         # key -> (canonical key, kind)
        self.stock = {}                         # stock_no -> canonical key
        self.unmapped_stock = []
        for r in items.itertuples():
            if isinstance(r.part_no, str) and r.part_no.strip():
                c = self.current(key(r.part_no))
                self.display.setdefault(key(r.part_no), r.part_no.strip())
                self.alias[key(r.part_no)] = (c, "self" if c == key(r.part_no) else "supersession")
                self.alias[c] = (c, "self")
                self.stock[r.stock_no] = c
            else:
                self.unmapped_stock.append(r.stock_no)
        for old, new in self.succ.items():
            self.alias[old] = (self.current(new), "supersession")
            self.alias.setdefault(self.current(new), (self.current(new), "self"))

    def current(self, k):
        seen = set()
        while k in self.succ and k not in seen:
            seen.add(k)
            k = self.succ[k]
        return k

    def name(self, k):
        return self.display.get(k, k)

    def __call__(self, raw):
        """-> (canonical P/N or None, reason or None)."""
        if raw is None or (isinstance(raw, float) and raw != raw) or not str(raw).strip():
            return None, "missing"
        k = key(raw)
        if k not in self.alias:
            return None, "unknown"
        c, kind = self.alias[k]
        canon = self.name(c)
        if kind == "supersession":
            return canon, "superseded"
        return canon, None if str(raw) == canon else "format"

    def from_stock(self, stock_no):
        c = self.stock.get(stock_no)
        return self.name(c) if c else None

    def infer_stock(self, stock_no, pn, votes, total):
        c = self.current(key(pn))
        self.stock[stock_no] = c
        self.alias.setdefault(c, (c, "self"))
        return dict(stock_no=stock_no, pn=self.name(c), votes=votes, total=total)


# ------------------------------------------------------------------ serials
def norm_serial(raw):
    """Upper case; in the numeric part O->0 and I/L->1; restore dropped leading zeros (5 digits)."""
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None
    s = str(raw).strip().upper()
    m = re.match(r"^([A-Z]{2})(.*)$", s)
    if not m:
        return s
    pre, rest = m.groups()
    rest = rest.replace("O", "0").replace("I", "1").replace("L", "1")
    if rest.isdigit() and len(rest) < 5:
        rest = rest.zfill(5)
    return pre + rest


class SerialResolver:
    def __init__(self):
        self.known = defaultdict(set)           # canonical P/N -> serials seen in clean systems

    def add(self, pn, sn):
        if pn and isinstance(sn, str) and sn:
            self.known[pn].add(sn)

    def __call__(self, pn, raw):
        """-> (serial, reason or None). reason: normalised | fuzzy | unknown | missing."""
        n = norm_serial(raw)
        if n is None:
            return None, "missing"
        pool = self.known.get(pn, set())
        if n in pool:
            return n, None if n == raw else "normalised"
        cand = [s for s in pool if DamerauLevenshtein.distance(n, s) == 1]
        if len(cand) == 1:
            return cand[0], "fuzzy"
        return n, "unknown"


# ------------------------------------------------------------------ text
GLOSSARY = [
    (r"\beng\b", "engine"), (r"\bhyd\b", "hydraulic"), (r"\bpress\b", "pressure"), (r"\btemp\b", "temperature"),
    (r"\brepl\b", "replaced"), (r"\bsat\b", "satisfactory"), (r"\binop\b", "inoperative"), (r"\bchk\b", "check"),
    (r"\bc/o\b", "carried out"), (r"\bt/s\b", "troubleshooting"), (r"\bcaut\b", "caution"), (r"\bfluct\b", "fluctuation"),
    (r"\bmn\b", "main"), (r"\bgbx\b", "gearbox"), (r"\bu/s\b", "unserviceable"), (r"\bind\b", "indication"),
    (r"\bvib\b", "vibration"), (r"\bg/run\b", "ground run"), (r"\bw/(?=\s)", "with"), (r"\bfnd\b", "found"),
]


def normalize_text(raw):
    if raw is None or (isinstance(raw, float) and raw != raw):
        return ""
    s = " " + str(raw).lower() + " "
    for pat, rep in GLOSSARY:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def majority(values):
    c = Counter(v for v in values if v)
    if not c:
        return None, 0, 0
    v, n = c.most_common(1)[0]
    return v, n, sum(c.values())
