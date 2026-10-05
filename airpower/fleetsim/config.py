"""Fleet configuration: YAML -> typed specs (see config/default.yaml and config/type_*.yaml)."""
import copy
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent / "config"


@dataclass
class InspectionSpec:
    id: str
    name: str
    level: str
    work_h: float
    detect_p: float
    interval_fh: float | None = None
    interval_days: float | None = None


@dataclass
class PositionSpec:
    type_id: str
    pos: str
    name: str
    ata: int
    qty: int
    pn: str
    policy: str                       # HT hard-time | OC on-condition | CM condition-monitored
    clock: str                        # fh | cycles
    level: str
    mttr_h: float
    cost: float
    weibull: tuple | None = None
    pf: float = 0.0
    life: dict = field(default_factory=dict)
    tat: tuple | None = None
    lead: tuple | None = None
    consumable: bool = False
    nff: float = 0.0
    defer: float = 0.0
    detect: float | None = None
    hums: dict | None = None

    @property
    def rotable(self):
        return not self.consumable

    def weibull_mean(self):
        if not self.weibull:
            return None
        b, eta = self.weibull
        return eta * math.gamma(1.0 + 1.0 / b)


@dataclass
class TypeSpec:
    id: str
    name: str
    inspections: list
    snags: dict
    positions: list

    def position(self, pos):
        return next(p for p in self.positions if p.pos == pos)


@dataclass
class FleetConfig:
    raw: dict
    types: dict

    def __getattr__(self, k):
        try:
            return self.raw[k]
        except KeyError:
            raise AttributeError(k)

    def all_positions(self):
        return [p for t in self.types.values() for p in t.positions]

    def parts(self):
        """Unique part numbers with the specs that use them."""
        out = {}
        for p in self.all_positions():
            out.setdefault(p.pn, []).append(p)
        return out


def _type(path):
    d = yaml.safe_load(Path(path).read_text())
    insp = [InspectionSpec(**i) for i in d["inspections"]]
    pos = []
    for p in d["positions"]:
        p = dict(p)
        for k in ("weibull", "tat", "lead"):
            if p.get(k) is not None:
                p[k] = tuple(p[k])
        pos.append(PositionSpec(type_id=d["id"], **p))
    return TypeSpec(id=d["id"], name=d["name"], inspections=insp, snags=d["snags"], positions=pos)


def _merge(a, b):
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(name="default", overrides=None):
    path = Path(name)
    if not path.suffix:
        path = CONFIG_DIR / f"{name}.yaml"
    raw = _merge(yaml.safe_load(path.read_text()), overrides)
    types = {}
    for t in raw["types"]:
        spec = _type(path.parent / t)
        types[spec.id] = spec
    return FleetConfig(raw=raw, types=types)
