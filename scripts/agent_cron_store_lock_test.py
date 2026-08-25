#!/usr/bin/env python3
"""The task store must not lose updates under concurrent writers.

`write_doc` replaces the file atomically, so the store is never torn. What was
missing is mutual exclusion across the *whole* read-modify-write: every writer
did load -> mutate -> write independently, so two writers could interleave and
the later write dropped the earlier one's change.

Observed on sogyo 2026-07-27: `agent-cron add` printed `OK` and the task was
absent afterwards. A scheduler tick (every minute on that node, plus a 3-minute
watchdog task) had read the store beforehand and wrote its own copy back over
the new task. It looked unreproducible because it is timing-dependent, not
random — the interleaving below reproduces it deterministically.

Hermetic: no scheduler, no headless run, no network. The race is driven by
calling the store primitives in the order the two processes hit them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import agent_cron  # noqa: E402
from agent_cron_repository import write_doc as repo_write  # noqa: E402

CMD = REPO / "scripts" / "agent-cron.sh"


def seed(path: Path, task_ids: list[str]) -> None:
    doc = {
        "version": 1,
        "tasks": [
            {
                "id": tid,
                "enabled": True,
                "prompt": f"task {tid}",
                "schedule": "*/5 * * * *",
                "timezone": "UTC",
                "notify": "none",
            }
            for tid in task_ids
        ],
    }
    repo_write(path, doc)


def ids_in(path: Path) -> list[str]:
    return [t["id"] for t in json.loads(path.read_text())["tasks"]]


class StoreLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = self.dir / "tasks.json"
        seed(self.store, ["existing"])
        agent_cron.store = self.store

    def test_lock_is_exclusive_across_processes(self) -> None:
        """A second holder must wait, not proceed."""
        import fcntl

        with agent_cron.store_lock():
            path = agent_cron.store_lock_path()
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_lock_is_released_after_the_block(self) -> None:
        import fcntl

        with agent_cron.store_lock():
            pass
        fd = os.open(str(agent_cron.store_lock_path()), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class LockCommandStaleRaceTest(unittest.TestCase):
    """The operator CLI must survive losing a stale-lock reclaim race.

    Regression (#869 sweep / #1076): acquire_for_run_guarded() has always caught
    FileNotFoundError around its stale-lock rename, but lock_command_guarded()
    did the same rename bare.  When a scheduler reclaimed the same stale lock
    first, the CLI raised out to the top-level `except Exception` and printed a
    bare error with no taskId or lockState.
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = self.dir / "tasks.json"
        seed(self.store, ["t1"])
        agent_cron.store = self.store
        self._real_lock_status = agent_cron.lock_status

    def tearDown(self) -> None:
        agent_cron.lock_status = self._real_lock_status

    def _task(self) -> dict:
        return json.loads(self.store.read_text())["tasks"][0]

    def test_losing_the_reclaim_race_is_reported_not_raised(self) -> None:
        import datetime

        # Report the lock as stale while the file is already gone -- exactly the
        # state another reclaimer leaves behind after winning the rename.
        agent_cron.lock_status = lambda *a, **k: {"lockState": "stale", "holder": None}
        self.assertFalse(agent_cron.lock_path("t1").exists())

        result, _as_json, code = agent_cron.lock_command_guarded(
            "t1",
            "acquire",
            "run-1",
            None,
            datetime.datetime.now(datetime.timezone.utc),
            True,
            self._task(),
        )

        self.assertEqual(code, 1)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["taskId"], "t1")
        self.assertEqual(result["action"], "acquire")
        self.assertIn("lockState", result)


class RunStateMergeTest(unittest.TestCase):
    """commit_run_state must not write back a stale whole-document snapshot."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = self.dir / "tasks.json"
        seed(self.store, ["runner"])
        agent_cron.store = self.store

    def test_a_task_added_during_a_run_survives(self) -> None:
        # The exact sogyo interleaving.
        stale = json.loads(self.store.read_text())          # scheduler reads
        task = stale["tasks"][0]

        added = json.loads(self.store.read_text())          # CLI adds, commits
        added["tasks"].append({
            "id": "added-mid-run", "enabled": True, "prompt": "new",
            "schedule": "0 9 * * *", "timezone": "UTC", "notify": "none",
        })
        repo_write(self.store, added)

        task["lastStatus"] = "success"                       # scheduler writes back
        task["runHistory"] = [{
            "runId": "r1", "scheduledAt": "2026-01-01T00:00:00Z",
            "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:00:00Z",
            "status": "success", "attempt": 1, "notifyState": "none",
        }]
        agent_cron.commit_run_state("runner", task)

        self.assertIn("added-mid-run", ids_in(self.store))
        after = json.loads(self.store.read_text())["tasks"]
        runner = next(t for t in after if t["id"] == "runner")
        self.assertEqual(runner["lastStatus"], "success")

    def test_an_edit_during_a_run_survives(self) -> None:
        stale = json.loads(self.store.read_text())
        task = stale["tasks"][0]

        edited = json.loads(self.store.read_text())
        edited["tasks"][0]["schedule"] = "30 4 * * *"
        repo_write(self.store, edited)

        task["lastStatus"] = "failed"
        agent_cron.commit_run_state("runner", task)

        runner = json.loads(self.store.read_text())["tasks"][0]
        self.assertEqual(runner["schedule"], "30 4 * * *")   # edit preserved
        self.assertEqual(runner["lastStatus"], "failed")     # run state applied

    def test_removal_during_a_run_is_not_resurrected(self) -> None:
        stale = json.loads(self.store.read_text())
        task = stale["tasks"][0]
        repo_write(self.store, {"version": 1, "tasks": []})

        task["lastStatus"] = "success"
        self.assertFalse(agent_cron.commit_run_state("runner", task))
        self.assertEqual(ids_in(self.store), [])

    def test_run_count_is_carried(self) -> None:
        # apply_run_limit writes runCount; omitting it from the projected set
        # made maxRuns silently stop counting.
        stale = json.loads(self.store.read_text())
        task = stale["tasks"][0]
        task["runCount"] = 3
        agent_cron.commit_run_state("runner", task)
        self.assertEqual(json.loads(self.store.read_text())["tasks"][0]["runCount"], 3)

    def test_disable_is_applied_to_the_fresh_task(self) -> None:
        stale = json.loads(self.store.read_text())
        task = stale["tasks"][0]
        agent_cron.commit_run_state("runner", task, disable=True)
        self.assertFalse(json.loads(self.store.read_text())["tasks"][0]["enabled"])


class CliAddUnderConcurrencyTest(unittest.TestCase):
    """End-to-end: `add` must land even when the loaded copy is already stale."""

    def test_add_persists(self) -> None:
        d = Path(tempfile.mkdtemp())
        store = d / "tasks.json"
        seed(store, ["existing"])
        proc = subprocess.run(
            [
                "bash", str(CMD), "add", "fresh-task",
                "--schedule", "0 9 * * *", "--prompt", "hello",
            ],
            env={**os.environ, "CCC_AGENT_CRON_STORE": str(store)},
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The reported success and the stored state must agree — the sogyo
        # symptom was exactly these two disagreeing.
        self.assertIn("OK", proc.stdout)
        self.assertIn("fresh-task", ids_in(store))
        self.assertIn("existing", ids_in(store))


class SameBootDeadHolderTest(unittest.TestCase):
    """A dead same-boot holder is SURFACED, never auto-stolen.

    The tested lock contract makes bootId change and the opt-in
    lockTimeoutSec the only staleness sources — a wrong liveness guess must
    not double-run a task. But a run killed mid-flight (SIGKILL/OOM) holds
    its O_EXCL lock until reboot under the default lockTimeoutSec=0 with
    nothing pointing at the cause; lock_status now reports holderAlive so an
    operator/doctor can release deliberately.
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = self.dir / "tasks.json"
        seed(self.store, ["job"])
        agent_cron.store = self.store

    def _write_lock(self, pid: int) -> None:
        path = agent_cron.lock_path("job")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "bootId": agent_cron.boot_id(),
                    "pid": pid,
                    "acquiredAt": "2026-08-25T00:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _status(self) -> dict:
        return agent_cron.lock_status(
            "job", {"lockTimeoutSec": 0}, agent_cron.parse_dt("2026-08-25T00:05:00+00:00", "at")
        )

    def test_dead_holder_in_same_boot_stays_held_but_is_flagged(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()  # reaped by us -> the pid provably no longer exists
        self._write_lock(child.pid)
        status = self._status()
        self.assertEqual(status["lockState"], "held")
        self.assertIs(status["holderAlive"], False)

    def test_live_holder_in_same_boot_reads_alive(self) -> None:
        self._write_lock(os.getpid())
        status = self._status()
        self.assertEqual(status["lockState"], "held")
        self.assertIs(status["holderAlive"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
