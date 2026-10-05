"""Project paths and run-time settings.

Environment overrides:
    AEROTWIN_DATA   AeroTwin's data/ folder (large real datasets are read from there, not copied)
    AIRPOWER_DB     SQLAlchemy URL of the integrated store (default: SQLite under outputs/)
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
MANIFEST = DATA / "manifest.json"
OUTPUTS = ROOT / "outputs"
FLEETSIM_OUT = OUTPUTS / "fleetsim"

AEROTWIN_DATA = Path(os.environ.get("AEROTWIN_DATA", "F:/AeroTwin/sih26054_v2/data"))


def db_url() -> str:
    url = os.environ.get("AIRPOWER_DB")
    if url:
        return url
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(OUTPUTS / 'airpower.sqlite').as_posix()}"
