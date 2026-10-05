"""HUMS signals for condition-monitored positions, replayed from real or NASA data.

* cmapss    - an engine serial is given one of the C-MAPSS run-to-failure trajectories that
              data_prep/cmapss.py reserves for FleetSim (never used to fit models). Its age in
              flight hours maps to a trajectory cycle (age / hours_per_cycle); it fails at the
              end of the trajectory. Channels use the C-MAPSS sensor names (Saxena et al. 2008).
* vibration - a gearbox serial is given a fault mode from the CWRU (bearing) or SEU (gear)
              windows prepared by AeroTwin. Before its P point it reports healthy windows;
              between P and failure the features move linearly from a healthy window towards a
              window of its fault mode.
* battery   - a battery serial follows the capacity-fade curve of a NASA cell that reaches the
              end-of-life state of health; age in flight hours maps to a discharge cycle.

Without the datasets (`real=False`, or files missing) the same interfaces return parametric
stand-ins, so the simulator and CI run anywhere; the run metadata records which was used.
"""
import numpy as np

CMAPSS_NAMES = ["op1", "op2", "op3", "T2", "T24", "T30", "T50", "P2", "P15", "P30", "Nf", "Nc", "epr", "Ps30",
                "phi", "NRf", "NRc", "BPR", "farB", "htBleed", "Nf_dmd", "PCNfR_dmd", "W31", "W32"]
VIB_FEATURES = ["Vib_RMS", "Vib_Kurtosis", "crest", "env_spec_kurt"]
BATTERY_CELLS = ["B0005", "B0006", "B0007", "B0018", "B0043", "B0044"]
SYNTH_ENGINE = ["T50", "P30", "Nc", "phi"]


class SignalBank:
    def __init__(self, real=True):
        self.real = False
        self.notes = []
        if real:
            try:
                self._load_real()
                self.real = True
            except (FileNotFoundError, ImportError, KeyError) as e:
                self.notes.append(f"real signals unavailable ({e}); parametric stand-ins used")

    # ------------------------------------------------------------------ loading
    def _load_real(self):
        from data_prep import cmapss, sources
        self.traj = {}
        for sub in cmapss.FLEET_SUBSETS:
            t = cmapss.fleet_trajectories(sub)
            self.traj[sub] = {u: d[cmapss.SETTINGS + cmapss.SENSORS].to_numpy(dtype=float) for u, d in t.items()}
        w = sources.load_vibration_windows()
        self.vib = {}
        for (ds, lab), g in w.groupby(["dataset", "label"]):
            self.vib.setdefault(ds, {})[lab] = g[VIB_FEATURES].to_numpy(dtype=float)
        b = sources.load_battery_cycles()
        self.cells = {}
        for c in BATTERY_CELLS:
            d = b[(b["cell"] == c) & b["usable"]].sort_values("cycle")
            self.cells[c] = d["soh"].to_numpy(dtype=float)
        self.notes.append("real signals: C-MAPSS fleet-replay units, CWRU / SEU windows, NASA battery cells")

    # ------------------------------------------------------------------ per-serial assignment
    def assign(self, hums, rng):
        """Signal state for a new or renewed serial."""
        k = hums["kind"]
        if k == "cmapss":
            if self.real:
                units = sorted(self.traj[hums["subset"]])
                u = int(rng.choice(units))
                return {"kind": k, "subset": hums["subset"], "unit": u, "L": len(self.traj[hums["subset"]][u])}
            return {"kind": k, "subset": hums["subset"], "unit": -1, "L": int(rng.integers(150, 350)),
                    "base": rng.normal(0, 1, len(SYNTH_ENGINE))}
        if k == "vibration":
            if self.real:
                faults = sorted(l for l in self.vib[hums["dataset"]] if l != "healthy")
                return {"kind": k, "dataset": hums["dataset"], "label": str(rng.choice(faults))}
            return {"kind": k, "dataset": hums["dataset"], "label": "synthetic"}
        if k == "battery":
            if self.real:
                c = str(rng.choice(BATTERY_CELLS))
                soh = self.cells[c]
                L = int(np.argmax(soh < hums["eol_soh"])) if np.any(soh < hums["eol_soh"]) else len(soh)
                return {"kind": k, "cell": c, "L": max(L, 10)}
            return {"kind": k, "cell": "synthetic", "L": int(rng.integers(60, 160))}
        raise ValueError(k)

    @staticmethod
    def life(hums, sig):
        """Age (flight hours) at which the serial fails, if the signal defines it."""
        if sig["kind"] == "cmapss":
            return sig["L"] * hums["hours_per_cycle"]
        if sig["kind"] == "battery":
            return sig["L"] * hums["fh_per_cycle"]
        return None

    @staticmethod
    def p_point(hums, sig, ttf, pf_default):
        if sig["kind"] == "cmapss":
            return ttf - hums.get("pf_cycles", 60) * hums["hours_per_cycle"]
        if sig["kind"] == "battery":
            return ttf - 0.25 * ttf
        return ttf - pf_default

    # ------------------------------------------------------------------ emission
    def emit(self, hums, sig, age, ttf, p_point, rng, noise=0.01):
        """Channel values for one sortie of a serial at `age` flight hours."""
        k = sig["kind"]
        if k == "cmapss":
            c = min(int(age / hums["hours_per_cycle"]), sig["L"] - 1)
            if self.real:
                # replayed as recorded: C-MAPSS already contains sensor noise, and relative noise
                # would swamp channels whose degradation signal is tiny next to their level (Nf, NRf)
                row = self.traj[sig["subset"]][sig["unit"]][c]
                return dict(zip(CMAPSS_NAMES, row))
            d = max(0.0, (c / sig["L"]) - 0.5) * 2.0
            drift = np.array([25.0, -6.0, 40.0, -0.4]) * d ** 2
            base = np.array([1400.0, 553.0, 9050.0, 47.5]) + sig["base"] * np.array([3, 0.5, 5, 0.1])
            return dict(zip(SYNTH_ENGINE, base + drift + rng.normal(0, 1, 4) * np.array([2, 0.3, 4, 0.08])))
        if k == "vibration":
            d = 0.0 if age < p_point else min(1.0, (age - p_point) / max(ttf - p_point, 1e-9))
            if self.real:
                pool = self.vib[sig["dataset"]]
                h = pool["healthy"][rng.integers(len(pool["healthy"]))]
                f = pool[sig["label"]][rng.integers(len(pool[sig["label"]]))]
                x = (1.0 - d) * h + d * f
            else:
                x = np.array([0.1, 3.0, 3.5, 5.0]) * (1.0 + np.array([2.0, 1.5, 0.8, 1.2]) * d)
            return dict(zip(VIB_FEATURES, x * (1.0 + rng.normal(0, noise, len(x)))))
        if k == "battery":
            c = age / hums["fh_per_cycle"]
            if self.real:
                soh = self.cells[sig["cell"]]
                v = float(np.interp(c, np.arange(len(soh)), soh))
            else:
                v = 1.0 - (1.0 - hums["eol_soh"]) * c / sig["L"]
            return {"soh": v + rng.normal(0, noise)}
        raise ValueError(k)
