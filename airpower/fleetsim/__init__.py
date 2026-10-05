"""FleetSim: synthetic fleet with real-data HUMS signals and fragmented source exports."""
from .config import load as load_config


def make_policy(policy="P0", **kw):
    """A policy by name (P0 / P1 / P2); keyword arguments go to its constructor."""
    from .policies import POLICIES
    if policy == "P2":
        from .predictive import Predictive
        return Predictive(**kw)
    return POLICIES[policy](**kw)


def simulate(config="default", seed=1, policy="P0", real_signals=True, days=None, overrides=None, warmup_days=0,
             policy_args=None, state=None):
    from .engine import FleetSim
    cfg = load_config(config, overrides)
    pol = make_policy(policy, **(policy_args or {})) if isinstance(policy, str) else policy
    return FleetSim(cfg, seed=seed, policy=pol, real_signals=real_signals, days=days, warmup_days=warmup_days,
                    state=state).run()
