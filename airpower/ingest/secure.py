"""HMAC-SHA256 envelope for HUMS ground-station uploads and other machine-to-backend messages.

Copied from AeroTwin (sih26054_v2/edge/link.py, `SignedJson` and its JSON helpers), including
the fixes from AeroTwin's security review: timestamps outside +-window (past or future) are
rejected, nonces are remembered to stop replays, and NaN becomes JSON null instead of
breaking the canonical encoding.
"""
import collections
import hashlib
import hmac
import json
import os
import time

import numpy as np

REPLAY_ERRORS = {"bad signature", "stale message", "replayed message"}


def default_key(label="airpower-demo"):
    """Demonstration key only. A deployment loads keys from the key store (docs/SECURITY.md)."""
    return hashlib.sha256(label.encode()).digest()


class SignedJson:
    def __init__(self, key, kid="k1", window_s=30.0):
        self.key, self.kid, self.window = key, kid, window_s
        self.seen = collections.OrderedDict()

    def seal(self, body, now=None):
        env = {"v": 1, "kid": self.kid, "ts": round(now if now is not None else time.time(), 3),
               "nonce": os.urandom(8).hex(), "body": _nan_to_none(body)}
        msg = json.dumps(env, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)
        sig = hmac.new(self.key, msg.encode(), hashlib.sha256).hexdigest()
        return json.dumps({"env": env, "sig": sig}, separators=(",", ":"), default=_json_default)

    def open(self, text, now=None):
        obj = json.loads(text)
        env, sig = obj["env"], obj["sig"]
        msg = json.dumps(env, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)
        if not hmac.compare_digest(hmac.new(self.key, msg.encode(), hashlib.sha256).hexdigest(), sig):
            raise ValueError("bad signature")
        now = now if now is not None else time.time()
        if abs(now - env["ts"]) > self.window:
            raise ValueError("stale message")
        if env["nonce"] in self.seen:
            raise ValueError("replayed message")
        self.seen[env["nonce"]] = env["ts"]
        while len(self.seen) > 10000:
            self.seen.popitem(last=False)
        return env["body"]


def _nan_to_none(o):
    if isinstance(o, dict):
        return {k: _nan_to_none(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_nan_to_none(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return None if o != o else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


def _json_default(o):
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    raise TypeError(type(o))
