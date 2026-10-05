"""Phase 8: authentication, roles, audit log and request hardening.

Runs on an empty store (only the operational tables), so it needs no data: the access rules and the
audit chain are what is under test, and the guard refuses a request before any data is read.

    python -m pytest -q tests/test_security.py
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from airpower import audit
from airpower.model import migrate
from airpower.model import schema as S
from server import security
from server import users as U
from server.app import create_app, rule_for
from server.settings import Settings

PW = {"commander": "amber-falcon-quartz-77", "engineer": "birch-harbor-onyx-31", "logistics": "cedar-lagoon-raven-52",
      "analyst": "dune-meadow-umber-18", "admin": "ember-nimbus-willow-64"}
ROLE = {"commander": "commander", "engineer": "engineering_officer", "logistics": "logistics_officer",
        "analyst": "analyst", "admin": "admin"}
H = {"X-Requested-With": "airpower"}


@pytest.fixture()
def env(tmp_path):
    url = f"sqlite:///{(tmp_path / 's.sqlite').as_posix()}"
    eng = S.engine(url)
    migrate.upgrade(eng)
    for u, r in ROLE.items():
        U.create(eng, u, r, PW[u])
    cfg = Settings()
    cfg.auth = True
    app = create_app(url, auth=True, settings=cfg, warm=False)
    return app, eng, url


def login(app, user, password=None):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": user, "password": password or PW[user]}, headers=H)
    return c, r


def test_every_route_has_a_rule(env):
    app, _, _ = env
    for r in app.routes:
        path = getattr(r, "path", "")
        if path.startswith(("/api/", "/report/")):
            for m in getattr(r, "methods", set()) - {"HEAD", "OPTIONS"}:
                probe = path.replace("{tail}", "J1001").replace("{jid}", "x").replace("{username}", "u").replace("{path:path}", "p")
                assert rule_for(m, probe) is not None, f"{m} {path} has no access rule"
    assert rule_for("GET", "/api/healthmap") == "view"            # a prefix of another route must not make it public
    assert rule_for("GET", "/api/health") == "public"
    assert rule_for("GET", "/api/nothing-here") == "view" and rule_for("DELETE", "/api/readiness") is None


def test_unauthenticated_is_refused(env):
    app, _, _ = env
    c = TestClient(app)
    for path in ("/api/readiness", "/api/meta", "/api/risk", "/api/dq", "/api/plan", "/report/fleet", "/api/admin/users", "/api/healthmap"):
        assert c.get(path).status_code == 401, path
    assert c.get("/api/health").json() == {"status": "ok"}
    me = c.get("/api/auth/me").json()
    assert me["user"] is None and me["auth_enabled"] and "UNCLASSIFIED" in me["classification"]
    assert c.get("/api/docs").status_code in (401, 404)           # API documentation is not published when sign-in is on


def test_login_logout_and_cookie_flags(env):
    app, _, _ = env
    c, r = login(app, "commander")
    assert r.status_code == 200 and r.json()["user"]["role"] == "commander"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/auth/me").json()["user"]["username"] == "commander"
    assert c.post("/api/auth/logout", headers=H).status_code == 200
    assert c.get("/api/auth/me").json()["user"] is None


def test_roles(env):
    app, _, _ = env
    expect = {  # route -> roles allowed (anything else is 403)
        "/api/dq": {"analyst", "admin"},
        "/api/admin/users": {"admin"},
        "/api/admin/audit": {"admin"},
    }
    for user in ROLE:
        c, _ = login(app, user)
        for path, ok in expect.items():
            code = c.get(path).status_code
            assert (code == 403) == (ROLE[user] not in ok), (user, path, code)
        # decisions: engineering officers only; what-if: everyone but the administrator
        d = c.post("/api/plan/decision", json={"tail": "J1001", "job": "x", "decision": "accept"}, headers=H).status_code
        assert (d == 403) == (user != "engineer"), (user, d)
        w = c.post("/api/whatif", json={"levers": {}}, headers=H).status_code
        assert (w == 403) == (user == "admin"), (user, w)
        assert (c.get("/report/fleet").status_code == 403) == (user == "admin"), user


def test_csrf_header_required(env):
    app, _, _ = env
    c, _ = login(app, "engineer")
    assert c.post("/api/plan/decision", json={"tail": "J1001", "job": "x", "decision": "accept"}).status_code == 403
    assert c.post("/api/auth/logout").status_code == 403


def test_wrong_password_lockout_and_audit(env):
    app, eng, _ = env
    for _ in range(U.MAX_FAILS):
        _, r = login(app, "analyst", "not the password at all")
        assert r.status_code == 401 and r.json()["detail"] == "wrong user name or password"
    _, r = login(app, "analyst")                                   # now locked, even with the right password
    assert r.status_code == 401
    with eng.begin() as conn:
        conn.execute(text("update app_user set locked_until = null where username = 'analyst'"))
    assert login(app, "analyst")[1].status_code == 200
    _, r = login(app, "nobody", "whatever-long-enough-1")
    assert r.status_code == 401 and r.json()["detail"] == "wrong user name or password"   # same message: no user enumeration
    with eng.connect() as conn:
        rows = [tuple(x) for x in conn.execute(text("select action, outcome from audit_log")).fetchall()]
    assert sum(1 for a, o in rows if a == "login" and o == "failed") >= U.MAX_FAILS + 1 and ("login", "ok") in rows


def test_address_throttle(env):
    app, _, _ = env
    codes = [login(app, f"ghost{i}", "x" * 14)[1].status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_disabled_user_and_role_change_apply_at_once(env):
    app, eng, _ = env
    c, _ = login(app, "logistics")
    assert c.get("/api/dq").status_code == 403
    U.set_role(eng, "logistics", "analyst")
    assert c.get("/api/dq").status_code != 403                      # role re-read on every request
    U.set_disabled(eng, "logistics")
    assert c.get("/api/dq").status_code == 401


def test_tampered_or_foreign_session_rejected(env):
    app, _, _ = env
    c, _ = login(app, "commander")
    tok = c.cookies.get("airpower_session")
    forged = TestClient(app)
    forged.cookies.set("airpower_session", tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB"))
    assert forged.get("/api/readiness").status_code == 401
    other = security.Sessions("another-secret").issue("admin")
    forged.cookies.set("airpower_session", other)
    assert forged.get("/api/admin/users").status_code == 401
    expired = security.Sessions("x", seconds=-5)
    assert expired.read(expired.issue("admin")) is None


def test_password_hashing_and_policy():
    h = security.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and security.verify_password("correct horse battery", h)
    assert not security.verify_password("correct horse batterz", h) and not security.verify_password("x", "garbage")
    assert security.password_problem("short") and security.password_problem("aaaaaaaaaaaaaaaa")
    assert security.password_problem("alice-is-here-today", "alice") and security.password_problem("a-good-long-passphrase-1") is None


def test_admin_user_management(env):
    app, _, _ = env
    c, _ = login(app, "admin")
    r = c.post("/api/admin/users", json={"username": "new.user", "role": "analyst", "password": "weak"}, headers=H)
    assert r.status_code == 422
    r = c.post("/api/admin/users", json={"username": "new.user", "display_name": "New", "role": "analyst",
                                          "password": "a-fine-long-passphrase-9"}, headers=H)
    assert r.status_code == 200
    n, r2 = login(app, "new.user", "a-fine-long-passphrase-9")
    assert r2.json()["user"]["must_change"] is True
    assert n.get("/api/dq").status_code == 403 and n.get("/api/dq").json()["detail"] == "password change required"
    assert n.post("/api/auth/password", json={"current": "wrong", "new": "another-long-passphrase-2"}, headers=H).status_code == 403
    assert n.post("/api/auth/password", json={"current": "a-fine-long-passphrase-9", "new": "another-long-passphrase-2"},
                  headers=H).status_code == 200
    assert n.get("/api/dq").status_code != 403
    # cannot demote / disable yourself, cannot remove the last administrator
    assert c.patch("/api/admin/users/admin", json={"role": "analyst"}, headers=H).status_code == 409
    assert c.patch("/api/admin/users/admin", json={"disabled": True}, headers=H).status_code == 409
    assert c.patch("/api/admin/users/new.user", json={"role": "admin"}, headers=H).status_code == 200
    assert c.patch("/api/admin/users/new.user", json={"role": "bogus"}, headers=H).status_code == 422
    assert all("password_hash" not in u for u in c.get("/api/admin/users").json())


def test_audit_chain_and_tamper_evidence(env):
    app, eng, url = env
    login(app, "commander")
    c, _ = login(app, "admin")
    v = c.get("/api/admin/audit/verify").json()
    assert v["ok"] and v["rows"] >= 2
    rows = c.get("/api/admin/audit?action=login").json()
    assert rows["total"] >= 2 and all(r["action"] == "login" for r in rows["rows"])
    # the database refuses to change or remove audit rows
    with pytest.raises(Exception, match="append-only"):
        with eng.begin() as conn:
            conn.execute(text("update audit_log set actor = 'mallory' where id = 1"))
    with pytest.raises(Exception, match="append-only"):
        with eng.begin() as conn:
            conn.execute(text("delete from audit_log where id = 1"))
    # and if the triggers were bypassed, the chain shows where it was edited
    raw = sqlite3.connect(url.replace("sqlite:///", ""))
    raw.execute("drop trigger audit_log_no_update")
    raw.execute("update audit_log set actor = 'mallory' where id = 1")
    raw.commit()
    raw.close()
    ok, n, bad, why = audit.verify(eng)
    assert not ok and bad == 1 and "content" in why
    assert c.get("/api/admin/audit/verify").json()["ok"] is False


def test_hardening_headers_and_limits(env):
    app, _, _ = env
    c = TestClient(app)
    r = c.get("/api/health")
    for h in ("x-content-type-options", "x-frame-options", "content-security-policy", "referrer-policy", "x-request-id", "x-data-classification"):
        assert h in r.headers, h
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    big = c.post("/api/auth/login", content=b"x" * 300000, headers={**H, "Content-Type": "application/json"})
    assert big.status_code == 413
    assert c.post("/api/auth/login", content=b"{not json", headers={**H, "Content-Type": "application/json"}).status_code == 422
    assert c.get("/api/auth/me", headers={"X-Forwarded-Proto": "https"}).headers["strict-transport-security"]


def test_whatif_validation_and_ownership(env):
    app, _, _ = env
    c, _ = login(app, "analyst")
    for bad in ({"warp": 9}, {"flying": 99}, {"lead": "x"}, {"surge": {"start_day": 1, "days": 400, "factor": 1.4}},
                {"capacity": {"O": 50}}, {"stock": "none"}, {"policy": "P9"}, {"surge": {"start_day": 1, "days": 3, "factor": 1.4, "x": 1}}):
        assert c.post("/api/whatif", json={"levers": bad}, headers=H).status_code == 422, bad
    assert c.get("/api/whatif/doesnotexist").status_code == 404


def test_auth_off_is_explicit(tmp_path):
    url = f"sqlite:///{(tmp_path / 's.sqlite').as_posix()}"
    app = create_app(url, auth=False, warm=False)
    c = TestClient(app)
    me = c.get("/api/auth/me").json()
    assert me["auth_enabled"] is False and me["user"]["role"] == "dev"
    assert c.post("/api/auth/login", json={"username": "a", "password": "b"}, headers=H).status_code == 400
    assert c.get("/api/dq").status_code != 403                       # development user holds every permission


def test_client_address_behind_trusted_proxy(tmp_path):
    url = f"sqlite:///{(tmp_path / 's.sqlite').as_posix()}"
    cfg = Settings()
    cfg.auth, cfg.trusted_proxy = True, "testclient"
    # the test client connects from the host name "testclient", which is not an IP: use a network that matches nothing,
    # then one that does, by overriding the peer through the ASGI scope
    app = create_app(url, auth=True, settings=cfg, warm=False)
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "x", "password": "y" * 14}, headers={**H, "X-Forwarded-For": "10.1.2.3"})
    cfg2 = Settings()
    cfg2.auth, cfg2.trusted_proxy = True, "0.0.0.0/0"
    app2 = create_app(url, auth=True, settings=cfg2, warm=False)
    app2_client = TestClient(app2, client=("172.28.0.5", 5000))
    app2_client.post("/api/auth/login", json={"username": "x", "password": "y" * 14}, headers={**H, "X-Forwarded-For": "evil, 10.9.8.7"})
    with S.engine(url).connect() as conn:
        ips = [r[0] for r in conn.execute(text("select ip from audit_log where action = 'login'")).fetchall()]
    assert "10.9.8.7" in ips and "10.1.2.3" not in ips              # believed from a trusted proxy only, last entry wins


def test_backup_restore_roundtrip(env, tmp_path):
    from tools import backup as B
    app, eng, url = env
    login(app, "commander")
    out = tmp_path / "bk"
    assert B.backup(url, out, keep=3) == 0
    f = next(out.glob("airpower-*.sqlite"))
    ok, msg = B.verify_file(f)
    assert ok and "audit chain intact" in msg
    target = f"sqlite:///{(tmp_path / 'restored.sqlite').as_posix()}"
    assert B.restore(f, target, force=False) == 0
    assert B.restore(f, target, force=False) == 1                     # will not overwrite without --force
    assert audit.verify(S.engine(target))[0]
    raw = sqlite3.connect(f)                                          # a corrupted audit row is caught when verifying a backup
    raw.execute("drop trigger audit_log_no_update")
    raw.execute("update audit_log set outcome = 'failed' where id = 1")
    raw.commit()
    raw.close()
    assert B.verify_file(f)[0] is False


def test_demo_cert_and_secrets(tmp_path, monkeypatch):
    from tools import init_deploy as D
    monkeypatch.setattr(D, "DEPLOY", tmp_path)
    monkeypatch.setattr(D, "ROOT", tmp_path)
    assert D.main(["--demo-cert"]) == 0
    assert len((tmp_path / "secrets" / "airpower_secret.txt").read_text().strip()) == 64
    assert (tmp_path / "certs" / "server.crt").read_text().startswith("-----BEGIN CERTIFICATE")
    before = (tmp_path / "secrets" / "airpower_secret.txt").read_text()
    D.main([])
    assert (tmp_path / "secrets" / "airpower_secret.txt").read_text() == before     # kept unless --force


def test_model_files_are_verified_before_loading(tmp_path, monkeypatch):
    from airpower.health import artifacts as A
    monkeypatch.delenv("AIRPOWER_MODEL_KEY", raising=False)
    monkeypatch.delenv("AIRPOWER_VERIFY_MODELS", raising=False)
    f = A.save({"w": [1, 2, 3]}, tmp_path / "m.joblib")
    assert A.load(f) == {"w": [1, 2, 3]}
    f.write_bytes(f.read_bytes() + b"x")                              # a changed model is refused, not run
    with pytest.raises(A.ArtifactError, match="does not match"):
        A.load(f)
    g = tmp_path / "other.joblib"
    g.write_bytes(b"not a model")
    with pytest.raises(A.ArtifactError, match="not in the model manifest"):
        A.load(g)
    # no manifest: loaded with a warning, refused in strict mode
    A.save({"a": 1}, tmp_path / "d2" / "n.joblib")
    (tmp_path / "d2" / "MANIFEST.json").unlink()
    assert A.load(tmp_path / "d2" / "n.joblib") == {"a": 1}
    monkeypatch.setenv("AIRPOWER_VERIFY_MODELS", "strict")
    with pytest.raises(A.ArtifactError, match="strict"):
        A.load(tmp_path / "d2" / "n.joblib")


def test_signed_manifest(tmp_path, monkeypatch):
    from airpower.health import artifacts as A
    monkeypatch.setenv("AIRPOWER_MODEL_KEY", "release-key-1")
    f = A.save({"w": 1}, tmp_path / "m.joblib")
    assert A.load(f) == {"w": 1}
    # an attacker who replaces the model and rewrites the manifest without the key is caught
    import json
    f.write_bytes(b"evil")
    m = json.loads((tmp_path / "MANIFEST.json").read_text())
    m["files"]["m.joblib"]["sha256"] = A._sha(f)
    (tmp_path / "MANIFEST.json").write_text(json.dumps(m))
    with pytest.raises(A.ArtifactError, match="signature"):
        A.load(f)


def test_data_load_keeps_users_decisions_and_audit(env):
    """A data load rebuilds the derived tables only: who may sign in, who decided what, and the audit log stay."""
    app, eng, _ = env
    login(app, "commander")
    S.create_all(eng)                                              # derived tables exist (empty)
    with eng.begin() as conn:
        conn.execute(text("insert into meta (key, value) values ('probe', '1')"))
        conn.execute(text("insert into plan_decision (tail, job, decision, start_day, user, at) "
                          "values ('J1001', 'x', 'accept', 3, 'engineer', '2026-01-01 00:00:00')"))
    before = {t: eng.connect().execute(text(f"select count(*) from {t}")).scalar() for t in ("app_user", "audit_log", "plan_decision")}
    S.create_all(eng, reset=True)                                  # what every data load does first
    after = {t: eng.connect().execute(text(f"select count(*) from {t}")).scalar() for t in ("app_user", "audit_log", "plan_decision")}
    assert before == after and before["app_user"] == 5 and before["audit_log"] >= 1 and before["plan_decision"] == 1
    assert eng.connect().execute(text("select count(*) from meta")).scalar() == 0          # derived data is gone
    assert audit.verify(eng)[0]


def test_migration_of_an_older_store(tmp_path):
    """A store from before the audit log and the plan-decision date column is brought to the current version."""
    from sqlalchemy import create_engine, inspect
    url = f"sqlite:///{(tmp_path / 'old.sqlite').as_posix()}"
    old = create_engine(url)
    with old.begin() as c:
        c.execute(text("create table plan_decision (id integer primary key, tail varchar(10), job varchar(80), decision varchar(12), "
                       "start_day integer, note text, user varchar(40), at datetime)"))
        c.execute(text("insert into plan_decision (tail, job, decision, user, at) values ('J1001', 'x', 'accept', 'old', '2026-01-01 00:00:00')"))
    eng = S.engine(url)
    assert migrate.upgrade(eng) == migrate.CURRENT
    cols = {c["name"] for c in inspect(eng).get_columns("plan_decision")}
    assert "start_date" in cols
    assert eng.connect().execute(text("select count(*) from plan_decision")).scalar() == 1        # the old row survived
    assert eng.connect().execute(text("select value from schema_info where key = 'version'")).scalar() == str(migrate.CURRENT)
    with pytest.raises(Exception, match="append-only"):
        with eng.begin() as conn:
            audit.log(eng, "x", "probe")
            conn.execute(text("delete from audit_log"))
    assert migrate.upgrade(eng) == migrate.CURRENT                                                # idempotent
