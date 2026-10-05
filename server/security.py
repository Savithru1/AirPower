"""Authentication and authorisation primitives (standard library only).

* Passwords: scrypt (memory-hard) with a per-user random salt; constant-time comparison.
* Sessions: a signed token (HMAC-SHA256 over a JSON payload) in an HttpOnly, SameSite=Strict cookie.
  The user's role and disabled flag are re-read from the store on every request, so disabling a user
  or changing a role takes effect at once.
* Roles and the permissions each grants (least privilege; the admin administers, it does not decide
  maintenance plans).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SESSION_SECONDS = 8 * 3600
MIN_PASSWORD = 12

ROLES = {
    "commander": {"view", "export_reports", "run_whatif"},
    "engineering_officer": {"view", "export_reports", "run_whatif", "decide_plan"},
    "logistics_officer": {"view", "export_reports", "run_whatif"},
    "analyst": {"view", "export_reports", "run_whatif", "view_dq"},
    "admin": {"view", "view_dq", "manage_users", "view_audit"},
}
ROLE_LABEL = {"commander": "Commander", "engineering_officer": "Engineering officer", "logistics_officer": "Logistics officer",
              "analyst": "Analyst", "admin": "Administrator"}


def hash_password(password: str, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(n, r, p, base64.b64encode(salt).decode(), base64.b64encode(h).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, h = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=256 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


_DUMMY = None


def burn_password_check(password: str):
    """Spend the same time as a real check, so an unknown user is not distinguishable by timing."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    verify_password(password, _DUMMY)


def password_problem(password: str, username: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"use at least {MIN_PASSWORD} characters"
    if username and username.lower() in password.lower():
        return "must not contain the user name"
    if len(set(password)) < 5:
        return "too repetitive"
    return None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Sessions:
    """Signed session tokens. `secret` comes from AIRPOWER_SECRET; without it a random one is made at
    start-up (sessions then end when the server restarts)."""

    def __init__(self, secret: str | None = None, seconds: int = SESSION_SECONDS):
        self.ephemeral = not secret
        self.key = (secret or secrets.token_hex(32)).encode()
        self.seconds = seconds
        self.revoked: dict[str, float] = {}

    def issue(self, username: str) -> str:
        body = _b64(json.dumps({"u": username, "exp": int(time.time()) + self.seconds, "sid": secrets.token_hex(8)},
                               separators=(",", ":")).encode())
        return f"{body}.{_b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())}"

    def read(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest()), sig):
            return None
        try:
            d = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        now = time.time()
        for sid in [k for k, t in self.revoked.items() if t < now]:
            self.revoked.pop(sid, None)
        if d.get("exp", 0) < now or d.get("sid") in self.revoked:
            return None
        return d

    def revoke(self, token: str | None):
        d = self.read(token)
        if d:
            self.revoked[d["sid"]] = d["exp"]


def secret_from_env() -> str | None:
    """AIRPOWER_SECRET, or the file named by AIRPOWER_SECRET_FILE (a Docker / Kubernetes secret)."""
    f = os.environ.get("AIRPOWER_SECRET_FILE")
    if f and os.path.exists(f):
        return open(f, encoding="utf-8").read().strip()
    return os.environ.get("AIRPOWER_SECRET")


def has(role: str | None, permission: str) -> bool:
    return permission in ROLES.get(role or "", set())
