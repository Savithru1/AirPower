"""Maintenance policies compared in the fleet study (docs/IMPLEMENTATION_PLAN.md section 7).

P0 Reactive    repair on failure; hard-time items at their limit; inspections find what they find
P1 Preventive  P0 + every on-condition / condition-monitored item replaced at a fixed share of
               its mean life (pm_fraction) at the inspection before that age
P2 Predictive  predictive.py: HUMS predictions and life limits drive spares drawn before removal,
               removals timed to the spare and to inspections, and a rolling CP-SAT plan

A policy can (1) add replacements at an inspection, (2) choose which aircraft fly, (3) start its
own processes (a planner, for example), and (4) say how often it removes each part, which the
stores use to size their stock. A policy compared against stock sized for a different policy
would be judged on the stock, not on itself.
"""
import math

import numpy as np


class Policy:
    name = "base"
    observes = False                    # True: receives every HUMS record (observe)

    def start(self, sim):
        pass

    def daily(self, sim, day):
        """Called at 06:00 every day, before inspections fall due."""

    def observe(self, sim, ac, slot, serial, channels):
        """One HUMS record of a monitored position (only if `observes`)."""

    def inspection_extra(self, sim, ac, insp, slot, serial):
        return None

    def inspection_finding(self, sim, ac, insp, slot, serial):
        """An inspection found a developing fault: the reason to remove it now, or None to fly on."""
        return "inspection"

    def take_kit(self, sim, ac, slot):
        """A spare already drawn for this position: (event, in hand) or None."""
        return None

    def select(self, sim, unit, cands, n):
        """Default: the serviceable aircraft with the fewest recent hours fly (hour balancing)."""
        return sorted(cands, key=lambda ac: ac.recent_fh)[:n]

    def removal_life(self, sim, spec):
        """Mean clock (fh or landings) between removals of one installed unit, for stock sizing.
        Reactive: the mean life, shortened by the P-F interval for items found at inspection."""
        ml = sim.mean_life[spec.pn]
        if spec.pf > 0 and math.isfinite(ml):
            ml = max(ml - spec.pf, 0.5 * ml)
        return ml


class Reactive(Policy):
    name = "P0"


class Preventive(Policy):
    name = "P1"

    def _tau(self, sim, ac_unit, spec):
        look = min(i.interval_fh or 1e9 for i in sim.cfg.types[spec.type_id].inspections)
        if spec.clock == "cycles":
            look *= sim.cyc_per_fh[ac_unit] if ac_unit else 1.0
        return sim.cfg.preventive["pm_fraction"] * sim.mean_life[spec.pn], look

    def inspection_extra(self, sim, ac, insp, slot, s):
        spec = s.spec
        if spec.policy not in ("OC", "CM"):
            return None
        tau, look = self._tau(sim, ac.unit, spec)
        return "preventive" if s.clock() + look >= tau else None

    def removal_life(self, sim, spec):
        """Age replacement at tau: mean cycle = E[min(T, tau_eff)] = integral of survival to tau_eff,
        where tau_eff ~ tau - half an inspection interval (replacement happens at the inspection
        before tau)."""
        base = super().removal_life(sim, spec)
        if spec.policy not in ("OC", "CM") or not math.isfinite(base):
            return base
        unit = next(u["id"] for u in sim.cfg.units if u["type"] == spec.type_id)
        tau, look = self._tau(sim, unit, spec)
        tau_eff = max(tau - 0.5 * look, 1e-6)
        if spec.weibull:
            b, eta = spec.weibull
            t = np.linspace(0, tau_eff, 400)
            return float(np.trapezoid(np.exp(-(t / eta) ** b), t))
        r = np.random.default_rng([sim.seed, 98])
        lives = [sim.bank.life(spec.hums, sim.bank.assign(spec.hums, r)) for _ in range(200)]
        return float(np.mean(np.minimum(lives, tau_eff)))


POLICIES = {"P0": Reactive, "P1": Preventive}
