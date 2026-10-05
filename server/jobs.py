"""Background jobs for the API: what-if forecasts take seconds to a minute, so they run in a thread
(the replications themselves in a process pool, airpower/twin/whatif.py) and the client polls.
Finished results are cached on disk by store, levers, horizon and replications."""
import threading
import time
import traceback
import uuid

from airpower.twin import whatif as W

_JOBS = {}
_LOCK = threading.Lock()
MAX_REPS, MAX_DAYS = 40, 180
MAX_RUNNING, MAX_KEPT = 2, 100        # simulations running at once (whole server), finished jobs remembered


class Busy(Exception):
    pass


def submit(url, levers, days, reps, procs=8, owner="-"):
    days, reps = max(7, min(int(days), MAX_DAYS)), max(4, min(int(reps), MAX_REPS))
    jid = uuid.uuid4().hex[:12]
    job = dict(id=jid, status="running", started=time.time(), levers=levers, days=days, reps=reps, result=None, error=None, owner=owner)
    with _LOCK:
        running = [j for j in _JOBS.values() if j["status"] == "running"]
        if any(j["owner"] == owner for j in running):
            raise Busy("you already have a forecast running; wait for it to finish")
        if len(running) >= MAX_RUNNING:
            raise Busy("the server is running its maximum number of forecasts; try again in a minute")
        for old in sorted(_JOBS.values(), key=lambda j: j["started"])[:max(0, len(_JOBS) - MAX_KEPT)]:
            _JOBS.pop(old["id"], None)
        _JOBS[jid] = job

    def work():
        try:
            job["result"] = W.forecast(levers, url, days=days, reps=reps, procs=procs)
            job["status"] = "done"
        except Exception as e:                        # reported to the client, not raised in the thread
            job["status"], job["error"] = "failed", f"{e!r}"
            traceback.print_exc()
        job["seconds"] = round(time.time() - job["started"], 1)

    threading.Thread(target=work, daemon=True).start()
    return job


def get(jid):
    return _JOBS.get(jid)
