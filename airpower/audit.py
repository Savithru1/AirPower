"""Append-only, hash-chained audit log.

Each row stores the hash of the row before it and its own hash over (previous hash, time, actor,
role, action, target, detail, address, outcome). The database refuses UPDATE and DELETE on the table
(airpower/model/migrate.py); if that were bypassed, `verify` finds the first row whose hash no longer
matches. Writers are serialised by a lock (one server process); the chain is checked end to end by
GET /api/admin/audit/verify.

Recorded: sign-ins and failures, sign-outs, password changes, user changes, plan accept / override,
what-if runs, report exports, signed HUMS uploads (accepted and refused), data loads, access
denied. Reading the dashboard is not logged.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone

from sqlalchemy import text

_LOCK = threading.Lock()
GENESIS = "0" * 64


def _digest(prev, ts, actor, role, action, target, detail, ip, outcome):
    blob = json.dumps([prev, ts, actor, role, action, target, detail, ip, outcome], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")


def log(eng, actor, action, outcome="ok", role=None, target=None, detail=None, ip=None):
    """Append one event. Never raises into the caller's request: a failed audit write is reported to stderr
    and, for security-relevant actions, the caller decides (the server refuses plan decisions it cannot audit)."""
    detail_s = json.dumps(detail, default=str, sort_keys=True) if detail is not None else None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with _LOCK, eng.begin() as c:
        row = c.execute(text("select hash from audit_log order by id desc limit 1")).first()
        prev = row[0] if row else GENESIS
        ts = _ts(now)
        h = _digest(prev, ts, actor, role, action, target, detail_s, ip, outcome)
        c.execute(text("insert into audit_log (ts, actor, role, action, target, detail, ip, outcome, prev_hash, hash) "
                       "values (:ts, :actor, :role, :action, :target, :detail, :ip, :outcome, :prev, :h)"),
                  dict(ts=now, actor=actor, role=role, action=action, target=target, detail=detail_s, ip=ip,
                       outcome=outcome, prev=prev, h=h))
    return h


def verify(eng):
    """Recompute the chain. Returns (ok, rows checked, first bad row id or None, reason)."""
    with eng.connect() as c:
        rows = c.execute(text("select id, ts, actor, role, action, target, detail, ip, outcome, prev_hash, hash "
                              "from audit_log order by id")).fetchall()
    prev, last_id = GENESIS, 0
    for r in rows:
        ts = r[1] if isinstance(r[1], datetime) else datetime.fromisoformat(str(r[1]))
        if r[9] != prev:
            return False, len(rows), r[0], "previous hash does not match (a row before this one was removed or changed)"
        if _digest(r[9], _ts(ts), r[2], r[3], r[4], r[5], r[6], r[7], r[8]) != r[10]:
            return False, len(rows), r[0], "row content does not match its hash"
        if r[0] <= last_id:
            return False, len(rows), r[0], "ids out of order"
        prev, last_id = r[10], r[0]
    return True, len(rows), None, None
