"""Integrity of trained model files.

Models are stored with joblib (pickle), and loading a pickle runs whatever code it contains, so a
model file someone has replaced is a way to run code in the server. Every model written by the
training scripts is recorded in outputs/models/MANIFEST.json (SHA-256 and size), and `load` checks the file
against it first. With AIRPOWER_MODEL_KEY set, the manifest itself carries an HMAC, so replacing a model
and the manifest together needs that key as well.

* manifest present, file matches      -> loaded
* manifest present, file differs or is missing from it -> ArtifactError (nothing is loaded)
* no manifest                         -> loaded with a warning; AIRPOWER_VERIFY_MODELS=strict refuses instead

    python -m airpower.health.artifacts manifest    # record every model file now present (after a trusted training run)
    python -m airpower.health.artifacts verify
"""
import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path

import joblib

from airpower.config import OUTPUTS

MODELS = OUTPUTS / "models"
log = logging.getLogger("airpower")


class ArtifactError(RuntimeError):
    pass


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sign(files):
    key = os.environ.get("AIRPOWER_MODEL_KEY")
    if not key:
        return None
    return hmac.new(key.encode(), json.dumps(files, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def _manifest_path(directory):
    return Path(directory) / "MANIFEST.json"


def read_manifest(directory=MODELS):
    p = _manifest_path(directory)
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    sig = _sign(m["files"])
    if sig is not None and not hmac.compare_digest(sig, m.get("hmac") or ""):
        raise ArtifactError(f"{p} does not match its signature (AIRPOWER_MODEL_KEY): refusing to trust it")
    return m


def record(path):
    """Add (or update) one file in the manifest. Called by `save`, i.e. by trusted training code."""
    path = Path(path)
    m = None
    try:
        m = read_manifest(path.parent)
    except ArtifactError:
        pass
    files = (m or {"files": {}})["files"]
    files[path.name] = {"sha256": _sha(path), "bytes": path.stat().st_size}
    out = {"files": dict(sorted(files.items())), "hmac": _sign(files)}
    _manifest_path(path.parent).write_text(json.dumps(out, indent=1), encoding="utf-8")


def save(obj, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(obj, path)
    record(path)
    return path


def verify(path):
    """Raises ArtifactError unless the file matches its manifest entry (see the module note for the no-manifest case)."""
    path = Path(path)
    m = read_manifest(path.parent)
    if m is None:
        if os.environ.get("AIRPOWER_VERIFY_MODELS", "").lower() == "strict":
            raise ArtifactError(f"no model manifest in {path.parent}: refusing to load {path.name} (AIRPOWER_VERIFY_MODELS=strict)")
        log.warning("no model manifest in %s: %s loaded unverified (python -m airpower.health.artifacts manifest)", path.parent, path.name)
        return
    e = m["files"].get(path.name)
    if e is None:
        raise ArtifactError(f"{path.name} is not in the model manifest: refusing to load it")
    if _sha(path) != e["sha256"]:
        raise ArtifactError(f"{path.name} does not match the model manifest (changed since it was trained): refusing to load it")


def load(path):
    verify(path)
    return joblib.load(path)


def main(argv=None):
    cmd = (argv or sys.argv[1:] or ["verify"])[0]
    files = sorted(MODELS.glob("*.joblib"))
    if cmd == "manifest":
        out = {"files": {f.name: {"sha256": _sha(f), "bytes": f.stat().st_size} for f in files}}
        out["hmac"] = _sign(out["files"])
        _manifest_path(MODELS).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"recorded {len(files)} model files in {_manifest_path(MODELS)}" + (" (signed)" if out["hmac"] else ""))
        return 0
    bad = []
    for f in files:
        try:
            verify(f)
        except ArtifactError as e:
            bad.append(str(e))
    print("all model files match the manifest" if not bad else "\n".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
