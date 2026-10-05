"""Two-echelon spares: base stores, a depot store, depot repair shop and procurement.

* A demand at a base is met from its own stock; otherwise from the depot or the other base
  (lateral transfer); otherwise it is backordered. Backorders are filled first-in first-out as
  serviceable units appear anywhere.
* Removed rotables travel to the depot shop (capacity-limited), are repaired in the
  position's turnaround time (no-fault-found units faster) and return as new, or are condemned
  (probability condemn_p) and replaced by procurement. Consumables are scrapped and reordered
  one-for-one.
* A policy may draw a spare ahead of a predicted removal (a kit, policy P2). Kits queue behind
  backorders from aircraft already waiting, and a held kit can be handed to such an aircraft.
* Initial stock per part number = the Poisson quantile of pipeline demand (removal rate x
  pipeline time) at a service level per class (rotable / consumable), split between bases by
  usage, remainder at the depot. That is the usual single-item base-stock rule; it ignores
  what a backorder costs in aircraft, which is what Phase 5's METRIC-style allocation adds.
"""
import math
import zlib
from collections import defaultdict, deque

import numpy as np
from scipy.stats import poisson

DAY = 24.0


def _u(rng, lohi):
    return float(rng.uniform(lohi[0], lohi[1]))


class Stores:
    def __init__(self, sim):
        self.sim, self.env, self.cfg = sim, sim.env, sim.cfg
        self.rng = np.random.default_rng([sim.seed, 3])
        self._rngs = {}                 # per part number when the simulator runs with crn_streams
        self.locs = list(self.cfg.bases)
        self.depot = next(k for k, v in self.cfg.bases.items() if v.get("depot"))
        self.stock = defaultdict(lambda: defaultdict(list))
        self.target = defaultdict(dict)
        self.backorders = defaultdict(deque)
        self.in_repair = defaultdict(int)
        self.on_order = defaultdict(int)
        self.lead_f = float(self.cfg.stores.get("lead_factor", 1.0))      # what-if: repair and procurement times x f
        self._initial_stock()

    def r(self, pn):
        if not self.sim.crn:
            return self.rng
        if pn not in self._rngs:
            self._rngs[pn] = np.random.default_rng([self.sim.seed, 3, zlib.crc32(pn.encode())])
        return self._rngs[pn]

    # ------------------------------------------------------------------ sizing
    def demand_rates(self):
        """Expected removals per day of each part number at each base (failure + NFF + life)."""
        sim = self.sim
        rate = defaultdict(lambda: defaultdict(float))
        for u in self.cfg.units:
            t = self.cfg.types[u["type"]]
            fhd = sim.fh_per_day[u["id"]]
            for p in t.positions:
                ml = sim.policy.removal_life(sim, p)      # the policy in force decides how often parts leave
                per_clock = 1.0 / ml if math.isfinite(ml) else 0.0
                per_clock *= 1.0 + p.nff / max(1.0 - p.nff, 1e-9)
                clock_per_day = fhd * (sim.cyc_per_fh[u["id"]] if p.clock == "cycles" else 1.0)
                r = per_clock * clock_per_day
                lim_fh = p.life.get("fh")
                if lim_fh:
                    r = max(r, fhd / lim_fh)
                if p.life.get("days"):
                    r = max(r, 1.0 / p.life["days"])
                rate[p.pn][u["base"]] += r * p.qty * u["aircraft"]
        return rate

    def pipeline_days(self, spec):
        dl = self.cfg.delays
        if spec.consumable:
            return float(np.mean(spec.lead))
        st = self.cfg.stores
        return float(np.mean(spec.tat)) + 2 * float(np.mean(dl["transfer_days"])) + st["condemn_p"] * float(np.mean(st["rotable_lead"]))

    def _initial_stock(self):
        sl = self.cfg.stores["service_level"]
        rates = self.demand_rates()
        self.sizing = {}
        for pn, specs in self.sim.parts.items():
            spec = specs[0]
            by_base = rates[pn]
            total = sum(by_base.values())
            mean = total * self.pipeline_days(spec)
            level = sl["consumable"] if spec.consumable else sl["rotable"]
            n = int(poisson.ppf(level, mean)) if mean > 0 else 0
            override = (self.cfg.stores.get("stock_override") or {}).get(pn)
            if override is not None:          # total stock chosen outside the simulator (e.g. the METRIC allocation)
                n = int(override)
            by_loc = (self.cfg.stores.get("stock_by_loc") or {}).get(pn)
            if by_loc is not None:            # stock where the records hold it (twin started from the store)
                alloc = {loc: int(k) for loc, k in by_loc.items() if loc in self.locs}
                n = sum(alloc.values())
            else:
                alloc, left = {}, n
                for b, r in sorted(by_base.items(), key=lambda kv: -kv[1]):
                    k = int(math.floor(n * r / total * 0.7)) if total > 0 else 0
                    alloc[b] = k
                    left -= k
                alloc[self.depot] = alloc.get(self.depot, 0) + left
            self.sizing[pn] = dict(rate_per_day=total, pipeline_days=self.pipeline_days(spec), stock=n, alloc=alloc)
            for loc, k in alloc.items():
                self.target[loc][pn] = k
                for _ in range(k):
                    self.stock[loc][pn].append(self.sim.new_serial(spec))
            # units already in the repair / procurement pipeline when the records end (twin): each
            # returns after a uniformly drawn share of its turnaround or lead time
            pipe = (self.cfg.stores.get("pipeline") or {}).get(pn) or {}
            for kind, n_units in pipe.items():
                lead = spec.tat if kind == "in_repair" and spec.tat else (spec.lead if spec.consumable else self.cfg.stores["rotable_lead"])
                for _ in range(int(n_units)):
                    delay = _u(self.r(pn), lead) * self.lead_f * DAY * float(self.r(pn).random())
                    self.env.process(self._returning(self.sim.new_serial(spec), delay))

    # ------------------------------------------------------------------ demand
    def request(self, loc, pn, tail, wo, kit=False):
        """Returns (event that yields a serial, available_locally)."""
        sim = self.sim
        rec = dict(id=sim.nid("DM"), t=self.env.now, loc=loc, pn=pn, tail=tail, wo=wo, src=None,
                   t_filled=None, backordered=False, kit=kit)
        sim.log["demands"].append(rec)
        ev = self.env.event()
        if self.stock[loc][pn]:
            s = self.stock[loc][pn].pop()
            rec["src"] = loc
            self.env.process(self._deliver(ev, s, rec, _u(self.r(pn), self.cfg.delays["issue_h"])))
            return ev, True
        src = self._lateral(loc, pn)
        if src is not None:
            s = self.stock[src][pn].pop()
            rec["src"] = src
            self._txn("transfer_out", src, s, rec)
            self.env.process(self._deliver(ev, s, rec, _u(self.r(pn), self.cfg.delays["transfer_days"]) * DAY))
            return ev, False
        rec["backordered"] = True
        self.backorders[pn].append((rec, ev))
        return ev, False

    def _lateral(self, loc, pn):
        order = [self.depot] + [b for b in self.locs if b not in (loc, self.depot)]
        for src in order:
            if src != loc and self.stock[src][pn]:
                return src
        return None

    def _deliver(self, ev, s, rec, delay):
        yield self.env.timeout(delay)
        rec["t_filled"] = self.env.now
        self._txn("issue", rec["loc"], s, rec)
        ev.succeed(s)

    def _txn(self, kind, loc, s, rec=None, **kw):
        self.sim.log["txns"].append(dict(t=self.env.now, kind=kind, loc=loc, pn=s.pn, sn=s.sn,
                                         demand=rec["id"] if rec else None,
                                         tail=rec["tail"] if rec else kw.get("tail"),
                                         wo=rec["wo"] if rec else kw.get("wo")))

    # ------------------------------------------------------------------ supply
    def arrive(self, s, loc):
        """A serviceable unit is available at `loc`: fill the oldest backorder, else replenish."""
        q = self.backorders[s.pn]
        if q:
            i = next((k for k, (r, _) in enumerate(q) if not r.get("kit")), 0)    # aircraft waiting before kits
            rec, ev = q[i]
            del q[i]
            rec["src"] = loc
            delay = _u(self.r(s.pn), self.cfg.delays["issue_h"]) if rec["loc"] == loc else \
                _u(self.r(s.pn), self.cfg.delays["transfer_days"]) * DAY
            self.env.process(self._deliver(ev, s, rec, delay))
            return
        if loc == self.depot:
            short = [(self.target[b].get(s.pn, 0) - len(self.stock[b][s.pn]), b) for b in self.locs if b != self.depot]
            short = [x for x in short if x[0] > 0]
            if short:
                b = max(short)[1]
                self.env.process(self._ship(s, b))
                return
        self.stock[loc][s.pn].append(s)
        self._txn("receipt", loc, s)

    def waiting(self, pn):
        """Backorders of aircraft already waiting for this part (not kits)."""
        return [(r, ev) for r, ev in self.backorders[pn] if not r.get("kit")]

    def hand_over(self, s, held_at, rec, ev):
        """A held kit goes to an aircraft waiting on backorder `rec`."""
        self.backorders[s.pn].remove((rec, ev))
        self._txn("receipt", held_at, s)
        rec["src"] = held_at
        r = self.r(s.pn)
        delay = _u(r, self.cfg.delays["issue_h"]) if rec["loc"] == held_at else _u(r, self.cfg.delays["transfer_days"]) * DAY
        self.env.process(self._deliver(ev, s, rec, delay))

    def _returning(self, s, delay):
        yield self.env.timeout(delay)
        self.arrive(s, self.depot)

    def _ship(self, s, to):
        self._txn("transfer_out", self.depot, s)
        yield self.env.timeout(_u(self.r(s.pn), self.cfg.delays["transfer_days"]) * DAY)
        self.arrive(s, to)

    def unserviceable(self, s, loc, nff=False, tail=None, wo=None):
        self._txn("return_us", loc, s, tail=tail, wo=wo)
        if s.spec.consumable:
            self._txn("scrap", loc, s, tail=tail, wo=wo)
            self.env.process(self._procure(s.spec, loc, s.spec.lead, reason="consumption"))
        else:
            self.env.process(self._repair(s, loc, nff))

    def _repair(self, s, loc, nff):
        sim, st = self.sim, self.cfg.stores
        if loc != self.depot:
            yield self.env.timeout(_u(self.r(s.pn), self.cfg.delays["transfer_days"]) * DAY)
        t_in = self.env.now
        self.in_repair[s.pn] += 1
        with sim.shop.request(priority=1) as req:
            yield req
            tat = _u(self.r(s.pn), s.spec.tat) * DAY * (st["nff_tat_factor"] if nff else 1.0) * self.lead_f
            yield self.env.timeout(tat)
        self.in_repair[s.pn] -= 1
        condemned = (not nff) and self.r(s.pn).random() < st["condemn_p"]
        sim.log["repairs"].append(dict(t_in=t_in, t_out=self.env.now, pn=s.pn, sn=s.sn, nff=nff, condemned=condemned,
                                       rogue=s.rogue, cost=s.spec.cost * (0.05 if nff else 0.2)))
        if condemned:
            self._txn("condemn", self.depot, s)
            yield from self._procure(s.spec, self.depot, st["rotable_lead"], reason="condemned")
            return
        if not nff:
            sim.renew(s)
        self.arrive(s, self.depot)

    def _procure(self, spec, loc, lead, reason):
        self.on_order[spec.pn] += 1
        t0 = self.env.now
        yield self.env.timeout(_u(self.r(spec.pn), lead) * DAY * self.lead_f)
        self.on_order[spec.pn] -= 1
        s = self.sim.new_serial(spec)
        self.sim.log["procurements"].append(dict(t_order=t0, t_arrive=self.env.now, pn=spec.pn, sn=s.sn, loc=loc,
                                                 reason=reason, cost=spec.cost))
        self.arrive(s, loc)

    # ------------------------------------------------------------------ reporting
    def snapshot(self, d):
        log = self.sim.log["stock"]
        for loc in self.locs:
            for pn in self.sim.parts:
                bo = sum(1 for rec, _ in self.backorders[pn] if rec["loc"] == loc)
                log.append(dict(day=d, loc=loc, pn=pn, serviceable=len(self.stock[loc][pn]), backorders=bo,
                                in_repair=self.in_repair[pn] if loc == self.depot else 0,
                                on_order=self.on_order[pn] if loc == self.depot else 0))
