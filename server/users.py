"""User accounts: create, change, disable; first-run seeding; command line.

    python -m server.users list
    python -m server.users create --username a.kumar --role engineering_officer --name "A Kumar"   (asks for the password)
    python -m server.users set-role --username a.kumar --role analyst
    python -m server.users disable --username a.kumar        (enable: --enable)
    python -m server.users reset-password --username a.kumar

Every change from the command line is written to the audit log with the operating-system user as actor.
"""
import argparse
import getpass
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import text

from airpower.config import ROOT, db_url
from airpower.model import migrate
from airpower.model import schema as S
from airpower import audit
from server import security

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
MAX_FAILS, LOCK_MINUTES = 5, 5
DEMO_FILE = ROOT / "deploy" / "demo_users.yaml"


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get(eng, username):
    with eng.connect() as c:
        r = c.execute(text("select id, username, display_name, role, password_hash, disabled, must_change, failed_logins, "
                           "locked_until, last_login from app_user where username = :u"), {"u": username}).mappings().first()
    return dict(r) if r else None


def listing(eng):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(text("select id, username, display_name, role, disabled, must_change, "
                                                "failed_logins, locked_until, created, last_login from app_user order by id")).mappings()]


def create(eng, username, role, password, display_name=None, must_change=False):
    if not USERNAME.match(username or ""):
        raise ValueError("user name: 3-40 characters, lower-case letters, digits, . _ -")
    if role not in security.ROLES:
        raise ValueError(f"role must be one of {sorted(security.ROLES)}")
    problem = security.password_problem(password, username)
    if problem:
        raise ValueError(f"password: {problem}")
    if get(eng, username):
        raise ValueError("user already exists")
    with eng.begin() as c:
        c.execute(text("insert into app_user (username, display_name, role, password_hash, disabled, must_change, failed_logins, created) "
                       "values (:u, :n, :r, :h, :d, :m, 0, :t)"),
                  dict(u=username, n=(display_name or username)[:80], r=role, h=security.hash_password(password), d=False,
                       m=must_change, t=now()))


def set_password(eng, username, password, must_change=False):
    problem = security.password_problem(password, username)
    if problem:
        raise ValueError(f"password: {problem}")
    with eng.begin() as c:
        c.execute(text("update app_user set password_hash = :h, must_change = :m, failed_logins = 0, locked_until = null where username = :u"),
                  dict(h=security.hash_password(password), m=must_change, u=username))


def set_role(eng, username, role):
    if role not in security.ROLES:
        raise ValueError(f"role must be one of {sorted(security.ROLES)}")
    with eng.begin() as c:
        c.execute(text("update app_user set role = :r where username = :u"), dict(r=role, u=username))


def set_disabled(eng, username, disabled=True):
    with eng.begin() as c:
        c.execute(text("update app_user set disabled = :d where username = :u"), dict(d=disabled, u=username))


def record_failure(eng, username):
    """Count a failed sign-in; lock the account for LOCK_MINUTES after MAX_FAILS in a row."""
    with eng.begin() as c:
        c.execute(text("update app_user set failed_logins = failed_logins + 1 where username = :u"), {"u": username})
        n = c.execute(text("select failed_logins from app_user where username = :u"), {"u": username}).scalar()
        if n is not None and n >= MAX_FAILS:
            c.execute(text("update app_user set locked_until = :t, failed_logins = 0 where username = :u"),
                      dict(t=now() + timedelta(minutes=LOCK_MINUTES), u=username))
            return True
    return False


def record_success(eng, username):
    with eng.begin() as c:
        c.execute(text("update app_user set failed_logins = 0, locked_until = null, last_login = :t where username = :u"),
                  dict(t=now(), u=username))


def bootstrap(eng, demo=False, log=print):
    """On an empty user table: the demo accounts (only if asked) or the first administrator from the
    environment (AIRPOWER_ADMIN_PASSWORD, or AIRPOWER_ADMIN_PASSWORD_FILE), who must change the password at first sign-in."""
    if listing(eng):
        return
    if demo:
        for u in yaml.safe_load(DEMO_FILE.read_text(encoding="utf-8"))["users"]:
            create(eng, u["username"], u["role"], u["password"], u.get("display_name"))
        audit.log(eng, "system", "users_seeded", target="demo accounts", detail={"file": str(DEMO_FILE.name)})
        log("demo accounts created (deploy/demo_users.yaml): DO NOT use in a real deployment")
        return
    pw = os.environ.get("AIRPOWER_ADMIN_PASSWORD")
    f = os.environ.get("AIRPOWER_ADMIN_PASSWORD_FILE")
    if f and os.path.exists(f):
        pw = Path(f).read_text(encoding="utf-8").strip()
    if pw:
        create(eng, "admin", "admin", pw, "Administrator", must_change=True)
        audit.log(eng, "system", "users_seeded", target="admin", detail={"source": "environment"})
        log("administrator 'admin' created from the environment; the password must be changed at first sign-in")
    else:
        log("no users exist: set AIRPOWER_ADMIN_PASSWORD (or use `python -m server.users create`) to create the first administrator")


def _password(prompt="Password: "):
    a = getpass.getpass(prompt)
    if a != getpass.getpass("Repeat: "):
        raise ValueError("passwords differ")
    return a


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "create", "set-role", "disable", "reset-password"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--username")
    ap.add_argument("--role")
    ap.add_argument("--name")
    ap.add_argument("--enable", action="store_true")
    a = ap.parse_args(argv)
    eng = S.engine(a.db or db_url())
    migrate.upgrade(eng)
    actor = f"os:{getpass.getuser()}"
    try:
        if a.cmd == "list":
            for u in listing(eng):
                print(f"{u['username']:20s} {u['role']:22s} {'disabled' if u['disabled'] else 'active':9s} last sign-in {u['last_login'] or '-'}")
            return 0
        if not a.username:
            ap.error("--username is required")
        if a.cmd == "create":
            create(eng, a.username, a.role or "", _password(), a.name)
        elif a.cmd == "set-role":
            set_role(eng, a.username, a.role or "")
        elif a.cmd == "disable":
            set_disabled(eng, a.username, not a.enable)
        elif a.cmd == "reset-password":
            set_password(eng, a.username, _password(), must_change=True)
        audit.log(eng, actor, f"user_{a.cmd.replace('-', '_')}", target=a.username, detail={"role": a.role, "via": "cli"})
        print("done")
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
