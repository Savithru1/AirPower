"""Download the real-data sources listed in data/manifest.json into data/raw/<id>/.

Copied from AeroTwin (sih26054_v2/tools/fetch_real_data.py). Change: only entries with
location 'local' and at least one URL are fetched; 'aerotwin' entries are read in place
(data_prep/sources.py) and 'to_verify' entries are listed but never downloaded.

Resumable (HTTP Range), idempotent (a file whose size matches is skipped) and auditable:
every fetched file's size and SHA-256 are written to data/raw/_fetch_log.json, so a
training run can name exactly which bytes it was built from (docs/ROADMAP.md section 6,
lineage).

    python tools/fetch_real_data.py                 # everything
    python tools/fetch_real_data.py igra2 cwru_bearing
    python tools/fetch_real_data.py --list
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "manifest.json"
RAW = ROOT / "data" / "raw"
LOG = RAW / "_fetch_log.json"
UA = {"User-Agent": "airpower-data-fetch/1.0 (research)"}
CHUNK = 1 << 20


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def fetch(url, dest, size=None, retries=5):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if dest.exists() and (size is None or dest.stat().st_size == size):
        return "cached"
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers=dict(UA, **({"Range": f"bytes={have}-"} if have else {})))
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if have and r.status != 206:          # server ignored Range: restart
                    have = 0
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        b = r.read(CHUNK)
                        if not b:
                            break
                        f.write(b)
            if size is not None and part.stat().st_size != size:
                raise IOError(f"size {part.stat().st_size} != expected {size}")
            part.replace(dest)
            return "fetched"
        except Exception as e:                          # network hiccup: back off and resume
            print(f"  retry {attempt + 1}/{retries} {dest.name}: {e}", file=sys.stderr)
            time.sleep(min(60, 5 * 2 ** attempt))
    raise RuntimeError(f"failed: {url}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="source ids (default: all)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    sources = json.loads(MANIFEST.read_text())["sources"]
    if a.list:
        for s in sources:
            tot = sum(f.get("size") or 0 for f in s["files"])
            print(f"{s['id']:14s} {s['location']:9s} {s['status']:10s} {len(s['files']):3d} files "
                  f"{tot / 1e6:9.1f} MB+  {s['title']}")
        return 0
    chosen = [s for s in sources if (not a.ids or s["id"] in a.ids)
              and s["location"] == "local" and s["status"] != "to_verify" and any("url" in f for f in s["files"])]
    jobs = [(s["id"], f) for s in chosen for f in s["files"] if "url" in f]
    log = json.loads(LOG.read_text()) if LOG.exists() else {}

    def run(job):
        sid, f = job
        dest = RAW / sid / f["dest"]
        status = fetch(f["url"], dest, f.get("size"))
        key = f"{sid}/{f['dest']}"
        if status == "fetched" or key not in log:
            log[key] = dict(url=f["url"], bytes=dest.stat().st_size, sha256=_sha256(dest),
                            fetched=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        print(f"{status:7s} {key} ({dest.stat().st_size / 1e6:.1f} MB)", flush=True)

    # big files first so they overlap with the many small ones
    jobs.sort(key=lambda j: -(j[1].get("size") or 0))
    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(run, jobs))
    LOG.write_text(json.dumps(dict(sorted(log.items())), indent=1))
    print(f"done: {len(jobs)} files, log {LOG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
