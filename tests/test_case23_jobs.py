"""Case 23: a change SimRack carries on with is a job that SimRack keeps.

A caller that cannot wait out a long change, like the MCP server, sends it with
``Prefer: respond-async``. SimRack answers at once with a job, makes the change
in turn with every other change, and keeps how it ended at ``/api/jobs/{job}``,
so the caller, or another one after it restarts, can come back for it.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from simrack.api import serve
from tests.fakes import SlowProxmox, TempDir, make_manager

TEMPLATE = 320
BUILD = {"name": "demo", "recipe": "single-switch", "template_vmid": TEMPLATE}
#: A change SimRack refuses at once, for when a job only has to exist.
NO_SUCH_SANDBOX = "/api/sandboxes/ghost/teardown"
ASYNC = {"Prefer": "respond-async"}


class JobsCase(unittest.TestCase):
    """SimRack on fakes, whose clones wait to be let go."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.proxmox = SlowProxmox(templates=[TEMPLATE])
        self.addCleanup(self.proxmox.let_go.set)
        self.manager = make_manager(self.tmp, proxmox=self.proxmox)
        self.url = self.serve()

    def serve(self) -> str:
        """Start SimRack's web API on the test's manager, as a restart would."""
        httpd = serve(self.manager, "127.0.0.1", 0)
        httpd.log = lambda message: None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def ask(self, method: str, path: str, body: dict | None = None, headers: dict | None = None, *, url: str = "") -> tuple:
        """SimRack's answer: its status, its headers and its JSON."""
        request = urllib.request.Request(
            (url or self.url) + path,
            data=None if method == "GET" else json.dumps(body or {}).encode(),
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as reply:
                return reply.status, reply.headers, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, json.loads(error.read() or b"{}")

    def job(self, path: str, body: dict | None = None, *, url: str = "") -> str:
        status, _, job = self.ask("POST", path, body, ASYNC, url=url)
        self.assertEqual(status, 202, job)
        return job["job"]

    def ended(self, job: str, *, url: str = "") -> dict:
        status, _, view = self.ask("GET", f"/api/jobs/{job}?wait=10", url=url)
        self.assertEqual((status, view.get("state")), (200, "done"), view)
        return view


class TestAChangeAsAJob(JobsCase):
    def test_simrack_answers_at_once_with_a_job_and_keeps_how_the_change_ended(self):
        status, headers, job = self.ask("POST", "/api/sandboxes", BUILD, ASYNC)

        self.assertEqual(status, 202, job)
        self.assertRegex(job["job"], r"^[0-9a-f]{6}-[0-9]+$")
        self.assertEqual(headers["Location"], f"/api/jobs/{job['job']}")
        self.assertEqual(headers["Preference-Applied"], "respond-async")
        self.assertTrue(self.proxmox.cloning.wait(5), "the build started")
        status, _, running = self.ask("GET", f"/api/jobs/{job['job']}?wait=0.2")
        self.assertEqual((status, running["state"], running["what"]), (200, "running", "POST /api/sandboxes"), running)

        self.proxmox.let_go.set()
        done = self.ended(job["job"])

        self.assertEqual(done["status"], 200, done)
        self.assertEqual(done["result"]["name"], "demo")
        self.assertEqual(self.ask("GET", "/api/sandboxes/demo")[0], 200)

    def test_a_refused_change_keeps_simrack_s_words_and_status(self):
        self.manager.set_paused(True)

        done = self.ended(self.job("/api/sandboxes", BUILD))

        self.assertEqual(done["status"], 409, done)
        self.assertEqual(done["result"]["error"], "Changes are paused.")
        self.assertFalse(self.proxmox.cloning.is_set())

    def test_asking_after_a_job_simrack_never_had_says_so(self):
        status, _, problem = self.ask("GET", "/api/jobs/beef00-99")

        self.assertEqual(status, 404, problem)
        self.assertIn("no job beef00-99", problem["error"])
        self.assertIn("last 50 finished jobs", problem["detail"])

    def test_how_long_to_wait_is_a_number_of_seconds(self):
        job = self.job(NO_SUCH_SANDBOX)

        status, _, problem = self.ask("GET", f"/api/jobs/{job}?wait=soon")

        self.assertEqual(status, 400, problem)
        self.assertIn("wait", problem["error"])

    def test_only_changes_become_jobs(self):
        status, _, paused = self.ask("POST", "/api/pause", {"paused": True}, ASYNC)
        self.assertEqual((status, paused), (200, {"paused": True}), "a pause is never queued")
        status, _, problem = self.ask("POST", "/api/nowhere", {}, ASYNC)
        self.assertEqual(status, 404, problem)
        status, _, state = self.ask("GET", "/api/state", headers=ASYNC)
        self.assertEqual((status, state["paused"]), (200, True))

    def test_simrack_keeps_the_last_50_finished_jobs(self):
        jobs = []
        for _ in range(51):
            jobs.append(self.job(NO_SUCH_SANDBOX))
            self.ended(jobs[-1])

        self.assertEqual(self.ask("GET", f"/api/jobs/{jobs[0]}")[0], 404)
        self.assertEqual(self.ended(jobs[1])["status"], 404, "the job is kept: the change it ran was refused")


class TestJobsTakeTurns(JobsCase):
    def test_a_job_holds_back_a_change_from_the_page_until_it_ends(self):
        job = self.job("/api/sandboxes", BUILD)
        self.assertTrue(self.proxmox.cloning.wait(5))
        answered = []
        page = threading.Thread(target=lambda: answered.append(self.ask("POST", NO_SUCH_SANDBOX)))
        page.start()

        page.join(0.5)
        self.assertEqual(answered, [], "the page's change waits its turn")

        self.proxmox.let_go.set()
        page.join(10)
        self.assertEqual(answered[0][0], 404)
        self.assertEqual(self.ended(job)["status"], 200)

    def test_a_change_behind_another_is_still_answered_at_once(self):
        first = self.job("/api/sandboxes", BUILD)
        self.assertTrue(self.proxmox.cloning.wait(5))

        asked = time.monotonic()
        second = self.job(NO_SUCH_SANDBOX)

        self.assertLess(time.monotonic() - asked, 2)
        self.assertEqual(self.ask("GET", f"/api/jobs/{second}?wait=0.2")[2]["state"], "running", "it waits its turn")
        self.proxmox.let_go.set()
        self.assertEqual(self.ended(second)["status"], 404)
        self.assertEqual(self.ended(first)["status"], 200)


class TestARestart(JobsCase):
    def test_a_job_from_before_a_restart_never_names_a_new_one(self):
        old = self.job(NO_SUCH_SANDBOX)
        restarted = self.serve()
        new = self.job(NO_SUCH_SANDBOX, url=restarted)

        self.assertNotEqual(old.split("-")[0], new.split("-")[0])
        status, _, problem = self.ask("GET", f"/api/jobs/{old}", url=restarted)
        self.assertEqual(status, 404, problem)


if __name__ == "__main__":
    unittest.main()
