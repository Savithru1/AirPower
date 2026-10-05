"""Server settings from the environment (the deployment's single place for configuration).

AIRPOWER_DB                     SQLAlchemy URL of the store (default: SQLite under outputs/)
AIRPOWER_AUTH                   on (default) | off. Off is for local development only: the server then
                                refuses to listen on anything but loopback and the UI shows a warning
AIRPOWER_SECRET / _FILE         key that signs session cookies (without it, one is generated at start-up
                                and sessions end at restart)
AIRPOWER_ADMIN_PASSWORD / _FILE first administrator, created when the user table is empty
AIRPOWER_DEMO_USERS             1: seed deploy/demo_users.yaml on an empty user table (demos and tests only)
AIRPOWER_CLASSIFICATION         label shown on every screen, report and API response
AIRPOWER_SECURE_COOKIES         auto (default: Secure flag when served over HTTPS) | 1 | 0
AIRPOWER_MAX_BODY               largest request body accepted, bytes (default 262144)
AIRPOWER_TLS_CERT / _KEY        serve HTTPS directly (otherwise terminate TLS at a reverse proxy)
AIRPOWER_TRUSTED_PROXY         comma-separated addresses / networks whose X-Forwarded-For is believed (the TLS proxy);
                                empty: the connecting address is the client's
AIRPOWER_HUMS_KEY_LABEL         which key verifies signed HUMS uploads (airpower/ingest/secure.py)
AIRPOWER_LOG                    log level (default INFO)
"""
import os
from dataclasses import dataclass, field


def _flag(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    auth: bool = field(default_factory=lambda: os.environ.get("AIRPOWER_AUTH", "on").strip().lower() not in ("off", "0", "false", "no"))
    demo_users: bool = field(default_factory=lambda: _flag("AIRPOWER_DEMO_USERS", False))
    classification: str = field(default_factory=lambda: os.environ.get("AIRPOWER_CLASSIFICATION",
                                                                      "UNCLASSIFIED // SYNTHETIC DEMONSTRATION DATA"))
    secure_cookies: str = field(default_factory=lambda: os.environ.get("AIRPOWER_SECURE_COOKIES", "auto").strip().lower())
    max_body: int = field(default_factory=lambda: int(os.environ.get("AIRPOWER_MAX_BODY", "262144")))
    log_level: str = field(default_factory=lambda: os.environ.get("AIRPOWER_LOG", "INFO").upper())
    trusted_proxy: str = field(default_factory=lambda: os.environ.get("AIRPOWER_TRUSTED_PROXY", ""))
    tls_cert: str | None = field(default_factory=lambda: os.environ.get("AIRPOWER_TLS_CERT"))
    tls_key: str | None = field(default_factory=lambda: os.environ.get("AIRPOWER_TLS_KEY"))
