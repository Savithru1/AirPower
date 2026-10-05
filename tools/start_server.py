"""AirPower server launcher for cloud hosting (Render, Railway, Docker, VPS).

Ensures the SQLite store exists (unpacks deploy/seed/airpower.sqlite.gz if missing),
configures host and port from environment variables, seeds demo accounts, and starts uvicorn.
"""
import argparse
import gzip
import logging
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_GZ = ROOT / "deploy" / "seed" / "airpower.sqlite.gz"
DEFAULT_DB = ROOT / "outputs" / "airpower.sqlite"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("airpower.launcher")


def ensure_database():
    db_env = os.environ.get("AIRPOWER_DB", "")
    if db_env.startswith("sqlite:///"):
        db_path_str = db_env.replace("sqlite:///", "")
        db_path = Path(db_path_str)
    elif not db_env:
        db_path = DEFAULT_DB
    else:
        # Remote database (e.g. PostgreSQL)
        return

    if not db_path.exists() or db_path.stat().st_size == 0:
        if SEED_GZ.exists():
            log.info("Database %s not found. Restoring from seed archive %s...", db_path, SEED_GZ)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(SEED_GZ, "rb") as fin, open(db_path, "wb") as fout:
                shutil.copyfileobj(fin, fout)
            log.info("Seed database unpacked successfully (%d bytes).", db_path.stat().st_size)
        else:
            log.warning("Seed database %s not found.", SEED_GZ)


def main():
    parser = argparse.ArgumentParser(description="Start the AirPower server")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("AIRPOWER_DB", None))
    args = parser.parse_args()

    ensure_database()

    from server.app import create_app
    from server.settings import Settings
    import uvicorn

    cfg = Settings()
    if os.environ.get("AIRPOWER_DEMO_USERS", "1").strip().lower() in ("1", "true", "yes", "on"):
        cfg.demo_users = True

    app = create_app(args.db, settings=cfg)
    log.info("AirPower server starting on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, server_header=False)


if __name__ == "__main__":
    main()
