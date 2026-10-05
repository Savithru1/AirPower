"""Schema versions of the operational tables (users, audit log, plan decisions).

The derived tables (everything the integration pipeline and the analytics write) are rebuilt on every
load and carry no history, so they are not migrated. The operational tables hold data that must
survive upgrades, so each change to them is a numbered step here, applied in order and recorded in
`schema_info`. Steps are idempotent. Alembic was the plan's choice; with three small operational
tables and a rebuilt derived schema, this runner is enough and adds no dependency.

    python -m airpower.model.migrate [--db URL]      # bring a store to the current version
"""
import argparse
import sys

from sqlalchemy import inspect, text

from airpower.config import db_url
from airpower.model import schema as S

CURRENT = 3


def _version(eng):
    with eng.connect() as c:
        r = c.execute(text("select value from schema_info where key = 'version'")).first()
    return int(r[0]) if r else 0


def _set_version(eng, v):
    with eng.begin() as c:
        c.execute(text("delete from schema_info where key = 'version'"))
        c.execute(text("insert into schema_info (key, value) values ('version', :v)"), {"v": str(v)})


def _v2_plan_decision_start_date(eng):
    cols = {c["name"] for c in inspect(eng).get_columns("plan_decision")}
    if "start_date" not in cols:
        with eng.begin() as c:
            c.execute(text("alter table plan_decision add column start_date TIMESTAMP"))


def _v3_audit_append_only(eng):
    """The database refuses to change or delete audit rows (the hash chain detects it if it is bypassed)."""
    with eng.begin() as c:
        if eng.dialect.name == "sqlite":
            for op in ("update", "delete"):
                c.execute(text(f"create trigger if not exists audit_log_no_{op} before {op} on audit_log "
                               "begin select raise(abort, 'audit_log is append-only'); end"))
        elif eng.dialect.name == "postgresql":      # not exercised in this environment (no PostgreSQL available)
            c.execute(text("create or replace function audit_log_immutable() returns trigger as $$ "
                           "begin raise exception 'audit_log is append-only'; end; $$ language plpgsql"))
            c.execute(text("drop trigger if exists audit_log_no_change on audit_log"))
            c.execute(text("create trigger audit_log_no_change before update or delete on audit_log "
                           "for each row execute function audit_log_immutable()"))


STEPS = {2: _v2_plan_decision_start_date, 3: _v3_audit_append_only}


def upgrade(eng):
    """Create missing operational tables and apply the steps above the store's version. Returns the version."""
    ops = [t for t in S.Base.metadata.sorted_tables if t.info.get("operational")]
    S.Base.metadata.create_all(eng, tables=ops)
    v = _version(eng)
    for n in sorted(STEPS):
        if n > v:
            STEPS[n](eng)
            v = n
    if v != CURRENT or _version(eng) != CURRENT:
        _set_version(eng, CURRENT)
    return CURRENT


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    eng = S.engine(a.db or db_url())
    before = 0
    try:
        before = _version(eng)
    except Exception:
        pass
    print(f"schema version {before} -> {upgrade(eng)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
