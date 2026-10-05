"""Fleet and tail reports as printable HTML (A4; the browser's print saves them as PDF).

Adapted from AeroTwin (sih26054_v2/server/render.py: Jinja2, inline CSS, no external assets,
the same number formatting helpers); the content is AirPower's: fleet readiness and actions, and
the aircraft twin (configuration, life-limited parts, component health, inspections, defects).
"""
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).with_name("templates")
DASH = "–"


def num(v: Any, nd: int = 0) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return DASH
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def pct(v: Any, nd: int = 0) -> str:
    return DASH if v is None or (isinstance(v, float) and v != v) else f"{100.0 * float(v):.{nd}f} %"


def day(v: Any) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return DASH
    try:
        return pd.Timestamp(v).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(v)


def local(v: Any, offset_h: float = 5.5) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return DASH
    return (pd.Timestamp(v) + pd.Timedelta(hours=offset_h)).strftime("%d %b %Y %H:%M")


def records(df):
    if df is None:
        return []
    if isinstance(df, list):
        return df
    return df.astype(object).where(df.notna(), None).to_dict("records")


_ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
_ENV.filters.update(num=num, pct=pct, day=day, local=local)


def render(template: str, **ctx) -> str:
    return _ENV.get_template(template).render(**ctx)
