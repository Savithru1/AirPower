"""AirPower API (FastAPI) over the unified store.

    python -m server.app [--db URL] [--host 127.0.0.1] [--port 8000]

Read routes serve the readiness, availability, aircraft, defects, stock and data-quality
views. POST /api/ingest/hums accepts one HUMS download sealed with the HMAC envelope
(airpower/ingest/secure.py), the path a HUMS ground station would use. The built dashboard
(web/dist) is served at / when present. Times in the API are UTC ISO 8601.

Access control (docs/SECURITY.md): every /api and /report route has a rule in RULES (method, path,
permission); a route without a rule is refused. Sign-in sets a signed HttpOnly SameSite=Strict
cookie; users and roles are in the store (server/users.py, server/security.py); sensitive actions
go to the hash-chained audit log (airpower/audit.py). State-changing calls must carry the header
X-Requested-With: airpower. Settings come from the environment (server/settings.py).
"""
import argparse
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import insert, select, text

from airpower import audit
from airpower.config import OUTPUTS, ROOT, db_url
from airpower.ingest.resolve import key
from airpower.ingest.secure import REPLAY_ERRORS, SignedJson, default_key
from airpower.model import migrate
from airpower.model import schema as S
from airpower.twin import status as ST
from airpower.twin import views as TV
from airpower.twin import whatif as W
from server import jobs as JOBS
from server import security
from server import users as U
from server.reports import render as RP
from server.settings import Settings

UTC_OFFSET_H = 5.5          # display / "08:00 local" convention of the operating bases
WEB = ROOT / "web" / "dist"
COOKIE = "airpower_session"
log = logging.getLogger("airpower")

PUBLIC, ANY = "public", "any"
# (method, path, permission): first match wins. A path matches itself and anything below it.
# No rule = refused. `public` needs no session; `any` needs a session; otherwise the role must hold the permission.
RULES = [
    ("GET", "/api/health", PUBLIC), ("GET", "/api/ready", PUBLIC), ("GET", "/api/auth/me", PUBLIC),
    ("POST", "/api/auth/login", PUBLIC),
    ("POST", "/api/ingest/hums", PUBLIC),                       # sealed envelope (HMAC, timestamp, replay cache), not a session
    ("POST", "/api/auth/logout", ANY), ("POST", "/api/auth/password", ANY),
    ("GET", "/api/admin/users", "manage_users"), ("POST", "/api/admin/users", "manage_users"),
    ("PATCH", "/api/admin/users", "manage_users"),
    ("GET", "/api/admin/audit", "view_audit"),
    ("GET", "/api/dq", "view_dq"),
    ("POST", "/api/whatif", "run_whatif"), ("GET", "/api/whatif", "run_whatif"),
    ("POST", "/api/plan/decision", "decide_plan"),
    ("GET", "/report", "export_reports"),
    ("GET", "/api", "view"),
]
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def rule_for(method, path):
    for m, pref, perm in RULES:
        if m == method and (path == pref or path.startswith(pref + "/")):
            return perm
    return None


def _iso(v):
    if v is None or (isinstance(v, float) and v != v) or v is pd.NaT:
        return None
    return pd.Timestamp(v).isoformat()


def _json(df):
    df = df.copy()
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].map(_iso)
    return json.loads(df.to_json(orient="records", date_format="iso"))


class Store:
    def __init__(self, url):
        self.url = url
        self.eng = S.engine(url)

    def q(self, sql, **params):
        df = pd.read_sql(text(sql), self.eng, params=params)
        for c in df.columns:
            # SQLite returns datetimes as text; only text columns with a time-like name are converted
            # (reliability_part.installed, for example, is a count)
            if c in ("start", "end", "raised", "closed", "entered", "opened", "started", "takeoff", "landing", "ts",
                     "date", "at", "installed", "removed", "flight_start", "flight_end", "downloaded", "completed") \
                    and not pd.api.types.is_numeric_dtype(df[c]):
                df[c] = pd.to_datetime(df[c])
        return df

    def meta(self, k):
        r = self.q("select value from meta where key = :k", k=k)
        return json.loads(r["value"][0]) if len(r) and k == "integration_report" else (r["value"][0] if len(r) else None)

    @lru_cache(maxsize=1)
    def status(self):
        return self.q('select tail, start, "end", status, reason from aircraft_status order by tail, start')

    @lru_cache(maxsize=1)
    def aircraft(self):
        return self.q("select * from aircraft")

    @lru_cache(maxsize=1)
    def daily(self):
        return ST.daily(self.status(), self.aircraft(), utc_offset_h=UTC_OFFSET_H)

    def window_end(self):
        return self.status()["end"].max()

    def default_at(self):
        end = self.window_end() - pd.Timedelta(days=1)
        return end.normalize() + pd.Timedelta(hours=8 - UTC_OFFSET_H)

    def at_time(self, at):
        st = self.status()
        cur = st[(st["start"] <= at) & (st["end"] > at)]
        return cur.merge(self.aircraft()[["tail", "unit_id", "type_id", "base"]], on="tail")


def create_app(url=None, hums_key=None, auth=None, settings=None, warm=True):
    cfg = settings or Settings()
    if auth is not None:
        cfg.auth = auth
    store = Store(url or db_url())
    migrate.upgrade(store.eng)
    sessions = security.Sessions(security.secret_from_env())
    if cfg.auth:
        U.bootstrap(store.eng, demo=cfg.demo_users, log=log.warning)
        if sessions.ephemeral:
            log.warning("AIRPOWER_SECRET is not set: a random session key was generated; sessions end when the server restarts")
    else:
        log.warning("AUTHENTICATION IS OFF (AIRPOWER_AUTH=off): for local development only")

    def _warm():
        try:                                   # first health map of a date takes seconds: build the default one early
            _healthmap(store.default_at().isoformat())
        except Exception:
            pass

    @asynccontextmanager
    async def lifespan(_app):
        if warm:
            threading.Thread(target=_warm, daemon=True).start()
        yield

    app = FastAPI(title="AirPower API", version="0.2.0", lifespan=lifespan, docs_url="/api/docs" if not cfg.auth else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if not cfg.auth else None,
                  description="Integrated predictive maintenance and fleet availability (SIH 2026, PS 26249)")
    app.state.store = store
    app.state.cfg = cfg
    app.state.sessions = sessions
    app.state.verifier = SignedJson(hums_key or default_key(os.environ.get("AIRPOWER_HUMS_KEY_LABEL", "airpower-demo")))
    fails = defaultdict(deque)                 # sign-in failures per address (sliding window)

    trusted = []
    for item in [x.strip() for x in cfg.trusted_proxy.split(",") if x.strip()]:
        try:
            trusted.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("AIRPOWER_TRUSTED_PROXY: ignoring %r", item)

    def client_ip(request):
        """The client's address: the connecting address, or the last X-Forwarded-For entry when the connection
        comes from a trusted proxy (which sets that header itself, replacing what the client sent)."""
        peer = request.client.host if request.client else None
        if peer and trusted:
            try:
                if any(ipaddress.ip_address(peer) in n for n in trusted):
                    fwd = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
                    if fwd:
                        return fwd
            except ValueError:
                pass
        return peer

    def principal(request):
        """The signed-in user (dict) or None. With authentication off, a development user with every permission."""
        if not cfg.auth:
            return dict(username="dev", display_name="Development user", role="dev", must_change=False)
        d = sessions.read(request.cookies.get(COOKIE))
        if not d:
            return None
        u = U.get(store.eng, d["u"])
        return None if (u is None or u["disabled"]) else u

    def permitted(user, perm):
        return user is not None and (user["role"] == "dev" or security.has(user["role"], perm))

    def permissions(user):
        if user["role"] == "dev":
            return sorted(set().union(*security.ROLES.values()))
        return sorted(security.ROLES.get(user["role"], set()))

    def guard(request: Request):
        """Deny by default: every /api and /report route needs a rule, and the role must hold its permission."""
        path = request.url.path
        request.state.user = None
        if not (path.startswith("/api/") or path.startswith("/report/")):
            return
        perm = rule_for(request.method, path)
        if perm is None:
            raise HTTPException(403, "no access rule for this route")
        if request.method not in SAFE_METHODS and path != "/api/ingest/hums" \
                and request.headers.get("x-requested-with") != "airpower":
            raise HTTPException(403, "missing X-Requested-With header")
        if perm == PUBLIC:
            return
        user = principal(request)
        if user is None:
            raise HTTPException(401, "sign in required")
        request.state.user = user
        if user["must_change"] and path not in ("/api/auth/password", "/api/auth/logout"):
            raise HTTPException(403, "password change required")
        if perm != ANY and not permitted(user, perm):
            audit.log(store.eng, user["username"], "access_denied", "denied", role=user["role"], target=f"{request.method} {path}",
                      ip=client_ip(request))
            raise HTTPException(403, "your role does not allow this")

    app.router.dependencies.append(Depends(guard))

    def me_payload(user):
        return {"auth_enabled": cfg.auth, "classification": cfg.classification,
                "user": None if user is None else dict(username=user["username"], display_name=user["display_name"], role=user["role"],
                                                       role_label=security.ROLE_LABEL.get(user["role"], "Development"),
                                                       permissions=permissions(user), must_change=bool(user["must_change"]))}

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        t0 = time.time()
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > cfg.max_body:
            resp = JSONResponse({"detail": "request body too large"}, status_code=413)
        else:
            try:
                resp = await call_next(request)
            except Exception:
                log.exception("unhandled error, request %s %s %s", rid, request.method, request.url.path)
                resp = JSONResponse({"detail": "internal error", "id": rid}, status_code=500)
        h = resp.headers
        path = request.url.path
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["X-Request-ID"] = rid
        h["X-Data-Classification"] = cfg.classification
        if "content-security-policy" not in h:
            h["Content-Security-Policy"] = CSP
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            h["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/api/") or path.startswith("/report/") or path == "/":
            h["Cache-Control"] = "no-store"
        elif path.startswith("/assets/"):
            h["Cache-Control"] = "public, max-age=31536000, immutable"
        user = getattr(request.state, "user", None)
        log.info("%s %s %s %d %.0fms user=%s", rid, request.method, path, resp.status_code, 1000 * (time.time() - t0),
                 user["username"] if user else "-")
        return resp

    async def read_json(request, limit=None):
        """Request body as JSON, refusing bodies over the limit even when no length was declared."""
        limit = limit or cfg.max_body
        buf = b""
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                raise HTTPException(413, "request body too large")
        try:
            return json.loads(buf or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(422, "body must be JSON")

    def _at(at):
        if at is None:
            return store.default_at()
        try:
            return pd.Timestamp(at).tz_localize(None) if pd.Timestamp(at).tzinfo else pd.Timestamp(at)
        except ValueError:
            raise HTTPException(422, "at must be an ISO 8601 time")

    @app.get("/api/health")
    def health():
        """Liveness: the process answers. Nothing about the data."""
        return {"status": "ok"}

    @app.get("/api/ready")
    def ready():
        """Readiness: the store holds a complete integration (the integration report is written last)."""
        try:
            n = int(store.q("select count(*) n from aircraft")["n"][0])
            done = store.q("select count(*) n from meta where key = 'integration_report'")["n"][0] > 0
        except Exception:
            n, done = 0, False
        if not (n and done):
            raise HTTPException(503, "store is empty or being loaded")
        return {"status": "ready"}

    @app.get("/api/auth/me")
    def me(request: Request):
        return me_payload(principal(request))

    @app.post("/api/auth/login")
    async def login(request: Request):
        body = await read_json(request, 4096)
        username = str(body.get("username", "")).strip().lower()[:40]
        password = str(body.get("password", ""))[:256]
        ip = client_ip(request)
        if not cfg.auth:
            raise HTTPException(400, "authentication is off")
        recent = fails[ip]
        while recent and recent[0] < time.time() - 600:
            recent.popleft()
        if len(recent) >= 10:
            audit.log(store.eng, username or "?", "login_throttled", "denied", ip=ip)
            raise HTTPException(429, "too many attempts from this address; try again in a few minutes")
        u = U.get(store.eng, username)
        locked = bool(u and u["locked_until"] and pd.Timestamp(u["locked_until"]) > U.now())
        if u is None or u["disabled"] or locked:
            security.burn_password_check(password)
            recent.append(time.time())
            audit.log(store.eng, username or "?", "login", "failed", ip=ip,
                      detail={"reason": "locked" if locked else "unknown or disabled"})
            raise HTTPException(401, "wrong user name or password")
        if not security.verify_password(password, u["password_hash"]):
            recent.append(time.time())
            now_locked = U.record_failure(store.eng, username)
            audit.log(store.eng, username, "login", "failed", role=u["role"], ip=ip, detail={"locked": now_locked})
            raise HTTPException(401, "wrong user name or password")
        U.record_success(store.eng, username)
        audit.log(store.eng, username, "login", role=u["role"], ip=ip)
        secure = cfg.secure_cookies == "1" or (cfg.secure_cookies == "auto" and (
            request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"))
        resp = JSONResponse(me_payload(u))
        resp.set_cookie(COOKIE, sessions.issue(username), max_age=sessions.seconds, httponly=True, samesite="strict",
                        secure=secure, path="/")
        return resp

    @app.post("/api/auth/logout")
    def logout(request: Request):
        sessions.revoke(request.cookies.get(COOKIE))
        u = request.state.user
        audit.log(store.eng, u["username"], "logout", role=u["role"], ip=client_ip(request))
        resp = JSONResponse({"status": "signed out"})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/auth/password")
    async def change_password(request: Request):
        u = request.state.user
        if not cfg.auth:
            raise HTTPException(400, "authentication is off")
        body = await read_json(request, 4096)
        if not security.verify_password(str(body.get("current", "")), u["password_hash"]):
            audit.log(store.eng, u["username"], "password_change", "failed", role=u["role"], ip=client_ip(request))
            raise HTTPException(403, "current password is wrong")
        try:
            U.set_password(store.eng, u["username"], str(body.get("new", "")), must_change=False)
        except ValueError as e:
            raise HTTPException(422, str(e))
        audit.log(store.eng, u["username"], "password_change", role=u["role"], ip=client_ip(request))
        return {"status": "password changed"}

    # ------------------------------------------------------------------ administration
    @app.get("/api/admin/users")
    def admin_users():
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in U.listing(store.eng)]

    @app.post("/api/admin/users")
    async def admin_create_user(request: Request):
        a = request.state.user
        b = await read_json(request, 4096)
        try:
            U.create(store.eng, str(b.get("username", "")).strip().lower(), str(b.get("role", "")), str(b.get("password", "")),
                     str(b.get("display_name") or "")[:80] or None, must_change=True)
        except ValueError as e:
            raise HTTPException(422, str(e))
        audit.log(store.eng, a["username"], "user_create", role=a["role"], target=b["username"], detail={"role": b.get("role")},
                  ip=client_ip(request))
        return {"status": "created"}

    @app.patch("/api/admin/users/{username}")
    async def admin_change_user(username: str, request: Request):
        a = request.state.user
        b = await read_json(request, 4096)
        t = U.get(store.eng, username)
        if t is None:
            raise HTTPException(404, "unknown user")
        users = U.listing(store.eng)
        admins = [x for x in users if x["role"] == "admin" and not x["disabled"]]
        losing_admin = t["role"] == "admin" and not t["disabled"] and (
            ("role" in b and b["role"] != "admin") or b.get("disabled") is True)
        if username == a["username"] and ("role" in b or "disabled" in b):
            raise HTTPException(409, "you cannot change your own role or disable yourself")
        if losing_admin and len(admins) <= 1:
            raise HTTPException(409, "at least one active administrator must remain")
        try:
            if "role" in b:
                U.set_role(store.eng, username, str(b["role"]))
            if "disabled" in b:
                U.set_disabled(store.eng, username, bool(b["disabled"]))
            if b.get("password"):
                U.set_password(store.eng, username, str(b["password"]), must_change=True)
        except ValueError as e:
            raise HTTPException(422, str(e))
        audit.log(store.eng, a["username"], "user_change", role=a["role"], target=username,
                  detail={k: ("(set)" if k == "password" else v) for k, v in b.items()}, ip=client_ip(request))
        return {"status": "changed"}

    @app.get("/api/admin/audit")
    def admin_audit(action: str | None = None, actor: str | None = None, outcome: str | None = None,
                    limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)):
        sql, p = "select id, ts, actor, role, action, target, detail, ip, outcome from audit_log where 1 = 1", {}
        for col, v in (("action", action), ("actor", actor), ("outcome", outcome)):
            if v:
                sql += f" and {col} = :{col}"
                p[col] = v
        n = store.q(sql.replace("select id, ts, actor, role, action, target, detail, ip, outcome", "select count(*) n"), **p)["n"][0]
        rows = store.q(sql + " order by id desc limit :lim offset :off", lim=limit, off=offset, **p)
        return {"total": int(n), "rows": _json(rows)}

    @app.get("/api/admin/audit/verify")
    def admin_audit_verify():
        ok, n, bad, why = audit.verify(store.eng)
        return {"ok": ok, "rows": n, "first_bad_id": bad, "reason": why}

    @app.get("/api/meta")
    def meta():
        rep = store.meta("integration_report") or {}
        st = store.status()
        return {"run": store.meta("run"), "loaded_utc": store.meta("loaded_utc"), "utc_offset_h": UTC_OFFSET_H,
                "window": [_iso(st["start"].min()), _iso(st["end"].max())], "default_at": _iso(store.default_at()),
                "units": _json(store.q("select u.id, u.type_id, u.base, t.name type_name from unit u "
                                       "join aircraft_type t on t.id = u.type_id")),
                "tables": rep.get("tables", {})}

    @app.get("/api/readiness")
    def readiness(at: str | None = None):
        t = _at(at)
        cur = store.at_time(t)
        units = []
        for u, g in cur.groupby("unit_id"):
            c = g["status"].value_counts()
            units.append(dict(unit=u, type=g["type_id"].iloc[0], base=g["base"].iloc[0], aircraft=int(len(g)),
                              serviceable=int(c.get("SERVICEABLE", 0)), nmcm=int(c.get("NMCM", 0)),
                              nmcs=int(c.get("NMCS", 0)), scheduled=int(c.get("SCHEDULED", 0))))
        ac = cur[["tail", "unit_id", "type_id", "status", "reason", "start"]].rename(columns={"start": "since"})
        # what keeps aircraft on the ground: parts awaited and open work
        drivers = {}
        for r in cur[cur["status"] == "NMCS"].itertuples():
            for part in (r.reason or "").split("; "):
                pn = part.split(" ", 1)[1] if " " in part else part
                drivers.setdefault(pn, set()).add(r.tail)
        parts = store.q("select pn, description from part").set_index("pn")["description"].to_dict()
        top = sorted(([pn, parts.get(pn, pn), sorted(t_)] for pn, t_ in drivers.items()), key=lambda x: -len(x[2]))
        return {"at": _iso(t), "units": units, "aircraft": _json(ac.sort_values(["unit_id", "tail"])),
                "nmcs_drivers": [dict(pn=p, description=d, aircraft=a) for p, d, a in top[:10]]}

    @app.get("/api/availability")
    def availability(unit: str | None = None, days: int = Query(365, ge=1, le=3650)):
        d = store.daily()
        if unit:
            d = d[d["unit"] == unit]
        d = d[d["day"] >= d["day"].max() - pd.Timedelta(days=days - 1)]
        return _json(d.rename(columns=str.lower))

    @app.get("/api/aircraft")
    def aircraft(at: str | None = None):
        cur = store.at_time(_at(at))[["tail", "status", "reason"]]
        a = store.aircraft().merge(cur, on="tail", how="left")
        fh = store.q("select tail, sum(fh) fh, count(*) sorties, max(landing) last_flight from sortie group by tail")
        return _json(a.merge(fh, on="tail", how="left"))

    @app.get("/api/aircraft/{tail}")
    def aircraft_detail(tail: str, at: str | None = None):
        t = _at(at)
        tk = key(tail)
        a = store.aircraft()
        if tk not in set(a["tail"]):
            raise HTTPException(404, f"unknown aircraft {tail}")
        st = store.status()
        tl = st[(st["tail"] == tk) & (st["end"] > t - pd.Timedelta(days=90)) & (st["start"] <= t)]
        de = store.q("select defect_no, raised, closed, category, ata, ata_source, text, rectification, wo_id "
                     "from defect where tail = :t and duplicate_of is null and raised <= :at and "
                     "(closed is null or closed > :at) order by raised", t=tk, at=t.to_pydatetime())
        inst = store.q("select i.position, i.pn, p.description, i.sn, i.installed from installation i "
                       "left join part p on p.pn = i.pn where i.tail = :t and (i.removed is null or i.removed > :at) "
                       "and (i.installed is null or i.installed <= :at) order by i.position", t=tk, at=t.to_pydatetime())
        so = store.q("select count(*) sorties, sum(fh) fh from sortie where tail = :t and takeoff <= :at "
                     "and takeoff > :a0", t=tk, at=t.to_pydatetime(), a0=(t - pd.Timedelta(days=30)).to_pydatetime())
        ch = store.q("select distinct position, channel from health_indicator where tail = :t", t=tk)
        return {"aircraft": _json(a[a["tail"] == tk])[0], "at": _iso(t), "timeline_90d": _json(tl),
                "open_defects": _json(de), "configuration": _json(inst),
                "last_30d": {"sorties": int(so["sorties"][0] or 0), "fh": float(so["fh"][0] or 0)},
                "hums_channels": _json(ch)}

    @app.get("/api/aircraft/{tail}/health")
    def health_series(tail: str, position: str, channel: str):
        d = store.q("select ts, sn, value from health_indicator where tail = :t and position = :p and channel = :c "
                    "order by ts", t=key(tail), p=position, c=channel)
        return _json(d)

    @app.get("/api/defects")
    def defects(tail: str | None = None, ata: int | None = None, q: str | None = None,
                limit: int = Query(200, ge=1, le=2000)):
        sql = ("select defect_no, tail, raised, closed, category, ata, ata_source, text, text_norm, rectification, "
               "wo_id, duplicate_of from defect where 1 = 1")
        p = {}
        if tail:
            sql += " and tail = :tail"
            p["tail"] = key(tail)
        if ata is not None:
            sql += " and ata = :ata"
            p["ata"] = ata
        if q:
            sql += " and (lower(text_norm) like :q or lower(rectification) like :q)"
            p["q"] = f"%{q.lower()}%"
        sql += " order by raised desc limit :lim"
        p["lim"] = limit
        return _json(store.q(sql, **p))

    @app.get("/api/stock")
    def stock(location: str | None = None):
        last = store.q("select max(date) d from stock_snapshot")["d"][0]
        d = store.q("select s.location, s.pn, p.description, p.category, s.serviceable, s.dues_out, s.in_repair, "
                    "s.on_order from stock_snapshot s left join part p on p.pn = s.pn "
                    "where s.date = (select max(date) from stock_snapshot)")
        if location:
            d = d[d["location"] == location]
        return {"date": _iso(last), "rows": _json(d)}

    @app.get("/api/risk")
    def risk(at: str | None = None, tail: str | None = None, status: str | None = None,
             limit: int = Query(500, ge=1, le=5000)):
        """Latest risk record of every monitored component at `at` (highest risk first)."""
        t = _at(at)
        try:
            sql = "select * from risk_record where as_of <= :at"
            p = {"at": t.to_pydatetime()}
            if tail:
                sql += " and tail = :tail"
                p["tail"] = key(tail)
            r = store.q(sql, **p)
        except Exception:                       # risk not built for this store yet
            return {"at": _iso(t), "rows": []}
        if not len(r):
            return {"at": _iso(t), "rows": []}
        r["as_of"] = pd.to_datetime(r["as_of"])
        last = r.sort_values("as_of").groupby(["tail", "position", "kind"]).tail(1)
        # a component removed since its last record is no longer fitted: keep only the fitted serial
        inst = store.q("select tail, sn, installed, removed from installation")
        for c in ("installed", "removed"):
            inst[c] = pd.to_datetime(inst[c])
        fitted = inst[(inst["installed"].isna() | (inst["installed"] <= t)) & (inst["removed"].isna() | (inst["removed"] > t))]
        last = last.merge(fitted[["tail", "sn"]].drop_duplicates(), on=["tail", "sn"])
        if status:
            last = last[last["status"] == status]
        last = last.sort_values("p_fail_100fh", ascending=False).head(limit)
        last["explanation"] = last["explanation"].map(lambda s: json.loads(s) if isinstance(s, str) else None)
        return {"at": _iso(t), "rows": _json(last.drop(columns=["id"]))}

    @app.get("/api/reliability")
    def reliability():
        """A6 / A8 / A9: per-part reliability, rogue serials, repeat defects, fleet KPIs."""
        try:
            parts = store.q("select * from reliability_part order by unscheduled desc")
            rog = store.q("select * from rogue_candidate order by p_value")
            rep = store.q('select * from repeat_defect order by occurrences desc, "last" desc limit 100')
            k = store.q("select value from meta where key = 'fleet_kpis'")
        except Exception:
            return {"kpis": None, "parts": [], "rogues": [], "repeats": []}
        for c in ("first", "last"):
            if c in rep:
                rep[c] = pd.to_datetime(rep[c])
        if "last_removal" in rog:
            rog["last_removal"] = pd.to_datetime(rog["last_removal"])
        return {"kpis": json.loads(k["value"][0]) if len(k) else None, "parts": _json(parts), "rogues": _json(rog),
                "repeats": _json(rep)}

    def _meta_json(k):
        try:
            r = store.q("select value from meta where key = :k", k=k)
            return json.loads(r["value"][0]) if len(r) else None
        except Exception:
            return None

    def _file_json(rel):
        p = OUTPUTS / rel
        return json.loads(p.read_text()) if p.exists() else None

    @app.get("/api/spares")
    def spares():
        """METRIC curve and targets, AOG risk, and the validation / backtest results when present."""
        try:
            tgt = store.q("select * from spares_target order by cost_lakh desc")
        except Exception:
            tgt = pd.DataFrame()
        try:
            aog = store.q("select * from aog_risk order by p_within_lead desc")
        except Exception:
            aog = pd.DataFrame()
        return {"metric": _meta_json("metric"), "targets": _json(tgt), "aog": _json(aog),
                "validation": _file_json("spares/metric_validation.json"),
                "forecast_backtest": _file_json("spares/forecast_backtest.json")}

    @app.get("/api/plan")
    def plan(which: str = Query("optimised", pattern="^(optimised|due_date)$")):
        summary = _meta_json("plan")
        daily = (_meta_json("plan_daily") or {}).get(which, [])
        try:
            jobs = store.q("select * from plan_job where plan = :p order by tail, start_day", p=which)
        except Exception:
            jobs = pd.DataFrame()
        return {"summary": summary, "daily": daily, "jobs": _json(jobs)}

    @app.get("/api/dq")
    def dq():
        rep = store.meta("integration_report") or {}
        by = store.q("select source, rule, severity, resolution, count(*) n from dq_issue "
                     "group by source, rule, severity, resolution order by source, n desc")
        return {"records_in": rep.get("records_in", {}), "convention_conversions": rep.get("convention_conversions", {}),
                "tables": rep.get("tables", {}), "issues": _json(by), "seconds": rep.get("seconds")}

    @app.get("/api/dq/issues")
    def dq_issues(rule: str | None = None, source: str | None = None, limit: int = Query(100, ge=1, le=1000),
                  offset: int = Query(0, ge=0)):
        sql = "select * from dq_issue where 1 = 1"
        p = {}
        if rule:
            sql += " and rule = :rule"
            p["rule"] = rule
        if source:
            sql += " and source = :source"
            p["source"] = source
        sql += " order by id limit :lim offset :off"
        p.update(lim=limit, off=offset)
        return _json(store.q(sql, **p))

    # ------------------------------------------------------------------ aircraft twin (Phase 7)
    def _plain(obj):
        """DataFrames and timestamps in a view -> JSON-ready values."""
        if isinstance(obj, pd.DataFrame):
            return _json(obj)
        if isinstance(obj, dict):
            return {k: _plain(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_plain(v) for v in obj]
        if isinstance(obj, pd.Timestamp):
            return _iso(obj)
        if isinstance(obj, float) and obj != obj:
            return None
        if hasattr(obj, "item"):                         # numpy scalars
            return obj.item()
        return obj

    @lru_cache(maxsize=16)
    def _healthmap(at_iso):
        return TV.health_map(store.q, pd.Timestamp(at_iso))

    @app.get("/api/healthmap")
    def healthmap(at: str | None = None):
        """Tails x systems: the worst finding per system (HUMS risk, life consumed), open defects,
        the next inspection and parts at risk of being awaited."""
        return _plain(_healthmap(_at(at).isoformat()))

    @lru_cache(maxsize=64)
    def _tail(tail, at_iso):
        return TV.tail_twin(store.q, tail, pd.Timestamp(at_iso))

    @app.get("/api/aircraft/{tail}/twin")
    def aircraft_twin(tail: str, at: str | None = None):
        """The aircraft twin: configuration, life-limited parts, component health, inspections due,
        defect history and recommended actions with their reasons."""
        try:
            return _plain(_tail(key(tail), _at(at).isoformat()))
        except KeyError:
            raise HTTPException(404, f"unknown aircraft {tail}")

    # ------------------------------------------------------------------ reports
    def _forecast_file():
        stem = Path(store.url.split("///")[-1]).stem
        return _file_json(f"twin/forecast_{stem}.json")

    def _report(request, template, target, **ctx):
        """Render a report with a per-response script nonce, the classification label and who prepared it;
        exports are audited (reports are the only way data leaves the system)."""
        u = request.state.user
        audit.log(store.eng, u["username"], "report_export", role=u["role"], target=target, ip=client_ip(request))
        nonce = secrets.token_urlsafe(16)
        html = RP.render(template, nonce=nonce, classification=cfg.classification, prepared_by=u["display_name"],
                         run=store.meta("run"), generated=pd.Timestamp.now("UTC").tz_localize(None), **ctx)
        return HTMLResponse(html, headers={"Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; img-src data:; frame-ancestors 'none'")})

    @app.get("/report/aircraft/{tail}", response_class=HTMLResponse, include_in_schema=False)
    def report_aircraft(tail: str, request: Request, at: str | None = None):
        try:
            t = _tail(key(tail), _at(at).isoformat())
        except KeyError:
            raise HTTPException(404, f"unknown aircraft {tail}")
        R = RP.records
        return _report(request, "aircraft.html", f"aircraft {t['aircraft']['tail']}", t=t, life=R(t["life"]), risk=R(t["risk"]),
                       ins=R(t["inspections"]), defects=R(t["defects"]), config=R(t["configuration"]))

    @app.get("/report/fleet", response_class=HTMLResponse, include_in_schema=False)
    def report_fleet(request: Request, at: str | None = None):
        t = _at(at)
        rd = readiness(t.isoformat())
        hm = _healthmap(t.isoformat())
        findings, actions = [], []
        for row in hm["rows"]:
            for sysname, c in row["cells"].items():
                if c and c.get("level", 0) > 0:
                    findings.append(dict(tail=row["tail"], system=sysname, level=c["level"], note=c["note"]))
            if any(c and c.get("level", 0) == 2 for c in row["cells"].values()):
                for x in _tail(row["tail"], t.isoformat())["actions"]:
                    if x["priority"] == 1:
                        actions.append(dict(x, tail=row["tail"]))
        findings.sort(key=lambda f: (-f["level"], f["tail"]))
        fc = _forecast_file()
        if fc:
            low = min(fc["baseline"]["daily"], key=lambda d: d["mean"])
            fc = dict(fc, low=dict(day=low["day"], mean=low["mean"], p10=low["p10"]))
        st = _file_json("twin/policy_study.json")
        if st:
            st = dict(st, order=[n for n in ("P0", "P1", "P2", "P0+METRIC", "P2+METRIC") if n in st["results"]])
        units = rd["units"]
        return _report(request, "fleet.html", "fleet", at=rd["at"], units=units, n_ac=sum(u["aircraft"] for u in units),
                       sv=sum(u["serviceable"] for u in units), nmcs=sum(u["nmcs"] for u in units),
                       nmcm=sum(u["nmcm"] for u in units), sched=sum(u["scheduled"] for u in units),
                       drivers=rd["nmcs_drivers"], findings=findings[:40], actions=actions[:30], forecast=fc, study=st)

    # ------------------------------------------------------------------ fleet twin: what-if and evidence (Phase 6)
    @app.get("/api/forecast")
    def forecast_default():
        """The fleet twin's forecast from the end of the records (no levers), when it has been built
        (python -m airpower.twin.whatif forecast)."""
        return _forecast_file() or {"status": "not built"}

    @app.post("/api/whatif")
    async def whatif(request: Request):
        """Start a what-if forecast: {"levers": {...}, "days": 90, "reps": 20}; poll /api/whatif/{id}."""
        u = request.state.user
        body = await read_json(request, 8192)
        try:
            levers = W.validate_levers(body.get("levers") or {})
        except (ValueError, TypeError, AttributeError) as e:
            raise HTTPException(422, str(e))
        try:
            job = JOBS.submit(store.url, levers, body.get("days", 90), body.get("reps", 20), owner=u["username"])
        except JOBS.Busy as e:
            raise HTTPException(429, str(e))
        audit.log(store.eng, u["username"], "whatif_run", role=u["role"], target=job["id"],
                  detail=dict(levers=levers, days=job["days"], reps=job["reps"]), ip=client_ip(request))
        return {k: job[k] for k in ("id", "status", "days", "reps", "levers")}

    @app.get("/api/whatif/{jid}")
    def whatif_status(jid: str, request: Request):
        job = JOBS.get(jid)
        if job is None or job["owner"] != request.state.user["username"]:       # a user sees only their own forecasts
            raise HTTPException(404, "unknown job")
        return {k: job.get(k) for k in ("id", "status", "days", "reps", "levers", "result", "error", "seconds")}

    @app.get("/api/whatif-levers")
    def whatif_levers():
        return {"defaults": W.DEFAULTS, "max_days": JOBS.MAX_DAYS, "max_reps": JOBS.MAX_REPS}

    @app.get("/api/study")
    def study():
        """Phase 6 evidence: the policy study in the simulated fleet and the twin's forecast check."""
        return {"study": _file_json("twin/policy_study.json"), "twin_validation": _file_json("twin/twin_validation.json")}

    # ------------------------------------------------------------------ plan decisions
    @app.post("/api/plan/decision")
    async def plan_decision(request: Request):
        """Accept a planned job, or override its day. Recorded with the user and time."""
        u = request.state.user
        b = await read_json(request, 8192)
        try:
            row = dict(tail=key(b["tail"]), job=str(b["job"])[:80], decision=b["decision"],
                       start_day=None if b.get("start_day") is None else int(b["start_day"]),
                       note=(str(b["note"])[:500] if b.get("note") else None), user=u["username"][:40],
                       at=pd.Timestamp.now("UTC").tz_localize(None).to_pydatetime())
            if row["decision"] not in ("accept", "override"):
                raise ValueError("decision must be accept or override")
            if row["decision"] == "override" and row["start_day"] is None:
                raise ValueError("an override needs start_day")
            if row["start_day"] is not None and not 0 <= row["start_day"] <= 400:
                raise ValueError("start_day out of range")
            if row["tail"] not in set(store.aircraft()["tail"]):
                raise ValueError("unknown aircraft")
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, str(e))
        # the date the decision fixes: the chosen day (override) or the planned day (accept) of the current plan
        plan = _meta_json("plan") or {}
        if row["start_day"] is None:
            try:
                pj = store.q("select start_day from plan_job where plan = 'optimised' and tail = :t and job = :j "
                             "and start_day is not null order by start_day limit 1", t=row["tail"], j=row["job"])
                row["start_day"] = int(pj["start_day"][0]) if len(pj) else None
            except Exception:
                pass
        row["start_date"] = ((pd.Timestamp(plan["t0"]) + pd.Timedelta(days=row["start_day"])).to_pydatetime()
                             if plan.get("t0") and row["start_day"] is not None else None)
        md = S.Base.metadata.tables
        # audit first: a decision that cannot be audited is not taken (the log is the record of who decided what)
        audit.log(store.eng, u["username"], "plan_" + row["decision"], role=u["role"], target=f"{row['tail']} {row['job']}",
                  detail=dict(start_day=row["start_day"], start_date=row["start_date"], note=row["note"]), ip=client_ip(request))
        with store.eng.begin() as c:
            row["id"] = (c.execute(text("select coalesce(max(id), 0) from plan_decision")).scalar() or 0) + 1
            c.execute(insert(md["plan_decision"]).values(**row))
        return _plain(row)

    @app.get("/api/plan/decisions")
    def plan_decisions():
        try:
            return _json(store.q("select * from plan_decision order by id"))
        except Exception:
            return []

    @app.post("/api/ingest/hums")
    async def ingest_hums(request: Request):
        buf = b""
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > cfg.max_body:
                raise HTTPException(413, "request body too large")
        raw = buf.decode("utf-8", "replace")
        try:
            h = app.state.verifier.open(raw)
        except ValueError as e:
            audit.log(store.eng, "hums-uplink", "hums_ingest", "denied", ip=client_ip(request), detail={"reason": str(e)[:120]})
            raise HTTPException(401 if str(e) in REPLAY_ERRORS else 400, str(e))
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            audit.log(store.eng, "hums-uplink", "hums_ingest", "denied", ip=client_ip(request), detail={"reason": "malformed envelope"})
            raise HTTPException(400, f"malformed envelope: {e!r}")
        try:
            unit, msn = h["hums_unit"], h["airframe_msn"]
            fs, fe = pd.Timestamp(h["flight_start_utc"]).tz_localize(None), pd.Timestamp(h["flight_end_utc"]).tz_localize(None)
            channels = h["channels"]
            ok_name = lambda x: isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_#-]{1,24}", x) is not None   # noqa: E731
            if not isinstance(channels, dict) or len(channels) > 40 or not all(
                    ok_name(k) and isinstance(v, dict) and len(v) <= 60 and all(ok_name(n) for n in v)
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v.values()) for k, v in channels.items()):
                raise ValueError("channels must be {position: {channel: number}} with plain names")
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid HUMS record: {e!r}")
        a = store.aircraft()
        m = a[(a["hums_unit"] == unit) & (a["msn"] == str(msn))]
        if not len(m):
            raise HTTPException(422, "unknown HUMS unit / airframe MSN pair")
        tail = m["tail"].iloc[0]
        md = S.Base.metadata.tables
        hrt, sot = md["hums_record"], md["sortie"]
        with store.eng.begin() as c:
            # typed queries, so datetimes are bound in the store's own format
            dup = c.execute(select(hrt.c.id).where(hrt.c.hums_unit == unit,
                                                   hrt.c.flight_start == fs.to_pydatetime())).first()
            if dup:
                return {"status": "duplicate", "hums_record_id": dup[0], "tail": tail}
            so = c.execute(select(sot.c.ref).where(sot.c.tail == tail, sot.c.takeoff.between(
                (fs - timedelta(minutes=10)).to_pydatetime(), (fs + timedelta(minutes=10)).to_pydatetime()))).first()
            rid = (c.execute(text("select coalesce(max(id), 0) from hums_record")).scalar() or 0) + 1
            c.execute(insert(md["hums_record"]).values(id=rid, tail=tail, hums_unit=unit, sortie_ref=so[0] if so else None,
                                                       flight_start=fs.to_pydatetime(), flight_end=fe.to_pydatetime(),
                                                       downloaded=pd.Timestamp.now("UTC").tz_localize(None).to_pydatetime(),
                                                       source_file="api:ingest/hums"))
            hid = (c.execute(text("select coalesce(max(id), 0) from health_indicator")).scalar() or 0)
            rows = []
            for pos, ch in channels.items():
                for name, v in ch.items():
                    hid += 1
                    rows.append(dict(ts=fe.to_pydatetime(), id=hid, hums_record_id=rid, tail=tail, position=pos,
                                     sn=(h.get("serials") or {}).get(pos), channel=name, value=float(v)))
            if rows:
                c.execute(insert(md["health_indicator"]), rows)
        return {"status": "ok", "hums_record_id": rid, "tail": tail, "sortie_ref": so[0] if so else None,
                "indicators": len(rows)}

    if WEB.exists() and (WEB / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "report/")):
                raise HTTPException(404, "not found")
            f = (WEB / path).resolve()
            if WEB.resolve() not in f.parents and f != WEB.resolve():          # no path traversal out of web/dist
                raise HTTPException(404, "not found")
            return FileResponse(f if path and f.is_file() else WEB / "index.html")

    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-auth", action="store_true", help="development only: no sign-in (loopback address only)")
    ap.add_argument("--demo-users", action="store_true", help="seed the demo accounts (deploy/demo_users.yaml) on an empty user table")
    a = ap.parse_args(argv)
    cfg = Settings()
    if a.no_auth:
        cfg.auth = False
    if a.demo_users:
        cfg.demo_users = True
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not cfg.auth and a.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("authentication is off: the server will only listen on a loopback address")
    tls = dict(ssl_certfile=cfg.tls_cert, ssl_keyfile=cfg.tls_key) if cfg.tls_cert and cfg.tls_key else {}
    uvicorn.run(create_app(a.db, settings=cfg), host=a.host, port=a.port, server_header=False, **tls)


if __name__ == "__main__":
    main()
