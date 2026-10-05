"""Changes SimRack carries on with after it has answered, kept for the caller to come back for.

A caller that cannot wait out a long change, like the MCP server, sends it with
``Prefer: respond-async`` and gets a job at once. SimRack keeps the job here
until the caller, or another one, asks how it ended. Jobs live in memory: a
restart forgets them, and the next start names its jobs afresh.
"""

from __future__ import annotations

import itertools
import math
import secrets
import threading
from dataclasses import dataclass, field

from .errors import LabError, NotFound

#: Finished jobs kept for callers to come back for; the oldest go first.
KEPT = 50
#: The longest one ask waits for a job to end, so the answer comes back inside a tunnel's or proxy's limit.
LONGEST_WAIT = 60.0


@dataclass
class Job:
    name: str
    #: The request that started it, like ``POST /api/sandboxes``.
    what: str
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0
    result: object = None

    def view(self) -> dict:
        if not self.done.is_set():
            return {"job": self.name, "what": self.what, "state": "running"}
        return {"job": self.name, "what": self.what, "state": "done", "status": self.status, "result": self.result}


class Jobs:
    def __init__(self) -> None:
        #: New each time SimRack starts, so a job named before a restart never names a later one.
        self.boot = secrets.token_hex(3)
        self.numbers = itertools.count(1)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def start(self, what: str, work) -> Job:
        """Run ``work`` on its own thread. It returns the HTTP status and the answer it would have sent."""
        with self.lock:
            job = Job(f"{self.boot}-{next(self.numbers)}", what)
            self.jobs[job.name] = job
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    def _run(self, job: Job, work) -> None:
        try:
            job.status, job.result = work()
        except Exception as error:  # noqa: BLE001 - a job must end, whatever went wrong
            job.status, job.result = 500, {"error": f"{type(error).__name__}: {error}"}
        with self.lock:
            job.done.set()
            for name in [name for name, kept in self.jobs.items() if kept.done.is_set()][:-KEPT]:
                del self.jobs[name]

    def wait(self, name: str, query: dict) -> dict:
        """The job, once it has ended or ``wait`` seconds have passed (at most a minute)."""
        with self.lock:
            job = self.jobs.get(name)
        if job is None:
            raise NotFound(
                f"There is no job {name}.",
                detail=f"SimRack keeps the last {KEPT} finished jobs until it restarts. Its page shows what was done.",
            )
        job.done.wait(seconds(query.get("wait", ["0"])[-1]))
        return job.view()


def seconds(text: str) -> float:
    try:
        wait = float(text)
    except ValueError:
        wait = math.nan
    if not math.isfinite(wait):
        raise LabError(f"wait must be a number of seconds, not {text!r}.", detail=f"SimRack waits at most {LONGEST_WAIT:g} seconds.")
    return min(max(wait, 0.0), LONGEST_WAIT)
