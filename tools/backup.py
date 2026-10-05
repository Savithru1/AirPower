"""Back up and restore the store (integrated data, users, plan decisions and the audit log).

    python -m tools.backup backup  [--db URL] [--dir outputs/backups] [--keep 14]
    python -m tools.backup restore FILE [--db URL]      (refuses to overwrite an existing store unless --force)
    python -m tools.backup verify  FILE                 (integrity check and audit-chain check)

SQLite: an online backup (safe while the server runs), checked with PRAGMA integrity_check and the
audit chain. PostgreSQL: runs pg_dump (custom format) when it is installed; otherwise prints the command.
"""
import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from airpower.config import OUTPUTS, db_url


def _sqlite_path(url):
    u = make_url(url)
    return Path(u.database) if u.drivername.startswith("sqlite") else None


def verify_file(path):
    """Integrity check and audit-chain check of a SQLite file. Returns (ok, message)."""
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        r = con.execute("pragma integrity_check").fetchone()[0]
        if r != "ok":
            return False, f"integrity check: {r}"
        has = con.execute("select 1 from sqlite_master where name = 'audit_log'").fetchone()
    finally:
        con.close()
    if has:
        from airpower import audit
        from airpower.model import schema as S
        ok, n, bad, why = audit.verify(S.engine(f"sqlite:///{Path(path).as_posix()}"))
        if not ok:
            return False, f"audit chain broken at entry {bad}: {why}"
        return True, f"integrity ok, audit chain intact ({n} entries)"
    return True, "integrity ok (no audit log in this file)"


def backup(url, directory, keep):
    p = _sqlite_path(url)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if p is None:
        out = directory / f"airpower-{stamp}.dump"
        cmd = ["pg_dump", "--format=custom", "--file", str(out), url.replace("+psycopg", "")]
        if shutil.which("pg_dump") is None:
            print("pg_dump is not installed here; run:  " + " ".join(cmd[:4]) + " <connection URL>")
            return 2
        subprocess.run(cmd, check=True)
        print(f"wrote {out}")
        return 0
    out = directory / f"airpower-{stamp}.sqlite"
    src = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    ok, msg = verify_file(out)
    print(f"wrote {out}: {msg}")
    if not ok:
        out.unlink()
        return 1
    for old in sorted(directory.glob("airpower-*.sqlite"))[:-keep]:
        old.unlink()
    return 0


def restore(file, url, force):
    p = _sqlite_path(url)
    if p is None:
        print("restore into PostgreSQL:  pg_restore --clean --if-exists --dbname <connection URL> FILE")
        return 2
    ok, msg = verify_file(file)
    if not ok:
        print(f"the backup does not verify: {msg}")
        return 1
    if p.exists() and not force:
        print(f"{p} exists: stop the server and use --force to replace it")
        return 1
    p.parent.mkdir(parents=True, exist_ok=True)
    for ext in ("-wal", "-shm"):
        Path(str(p) + ext).unlink(missing_ok=True)
    shutil.copyfile(file, p)
    print(f"restored {file} -> {p} ({msg})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["backup", "restore", "verify"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--db", default=None)
    ap.add_argument("--dir", default=str(OUTPUTS / "backups"))
    ap.add_argument("--keep", type=int, default=14)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "backup":
        return backup(a.db or db_url(), a.dir, a.keep)
    if not a.file:
        ap.error("FILE is required")
    if a.cmd == "verify":
        ok, msg = verify_file(a.file)
        print(msg)
        return 0 if ok else 1
    return restore(a.file, a.db or db_url(), a.force)


if __name__ == "__main__":
    sys.exit(main())
