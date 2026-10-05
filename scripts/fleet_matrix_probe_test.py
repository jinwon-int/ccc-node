#!/usr/bin/env python3
"""Process and health contract for the Matrix fleet probe."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_matrix_probe as probe


class MatrixProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.data = self.root / "matrix"
        self.data.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"state_directory": str(self.state)}))
        self.now = time.time()

    def process(self, pid: int, *, channel: str, cgroup: str = "/") -> None:
        target = self.proc / str(pid)
        target.mkdir()
        (target / "cmdline").write_bytes(
            b"/venv/bin/python\0-m\0telegram_bot\0--path\0/home/x\0"
        )
        (target / "environ").write_bytes(
            f"CCC_CHANNEL={channel}\0BOT_DATA_DIR={self.data}\0"
            f"CCC_MATRIX_CONFIG_PATH={self.config}\0".encode()
        )
        (target / "cgroup").write_text(f"0::{cgroup}\n")

    def health(
        self, pid: int, state: str = "available", *, age: int = 0,
        error_age: int | None = None, ok_age: int | None = None, reason: str | None = None,
    ) -> None:
        path = self.data / "health.json"
        snapshot: dict = {
            "process": {
                "pid": pid,
                "started_at": datetime.fromtimestamp(
                    self.now - 600, timezone.utc
                ).isoformat(),
            },
            "service": {"state": state},
        }
        if error_age is not None:
            def stamp(seconds: int) -> str:
                return datetime.fromtimestamp(self.now - seconds, timezone.utc).isoformat()
            error = "agent turn failed: danso_provider_timeout / Worker failed"
            snapshot["agent"] = {
                "state": "degraded", "last_error": error, "last_error_at": stamp(error_age),
                "last_ok_at": stamp(ok_age) if ok_age is not None else None,
            }
            snapshot["service"]["reason"] = reason if reason is not None else f"Danso: {error}"
        path.write_text(json.dumps(snapshot))
        os.utime(path, (self.now - age, self.now - age))

    def db_health(self, state: str = "ready", *, age: int = 0) -> None:
        with sqlite3.connect(self.state / "inbox.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                ("health", json.dumps({"state": state, "updated": self.now - age})),
            )

    def test_older_telegram_does_not_mask_matrix(self) -> None:
        self.process(100, channel="telegram")
        self.process(200, channel="matrix")
        self.health(200)
        self.db_health()
        self.assertEqual(probe.probe(self.proc, now=self.now), ("OK", "db-ready"))

    def test_missing_matrix_is_down_even_when_telegram_runs(self) -> None:
        self.process(100, channel="telegram")
        self.assertEqual(probe.probe(self.proc), ("DOWN", "no-process"))

    def test_stale_health_uses_recent_matrix_sync_state(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, age=121)
        self.db_health()
        self.assertEqual(probe.probe(self.proc, now=self.now), ("OK", "db-ready"))

    def test_stale_db_is_unverified(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, age=121)
        self.db_health(age=121)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("UNVERIFIED", "matrix-db-stale"))

    def test_old_ready_db_cannot_validate_new_process(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="starting")
        path = self.data / "health.json"
        data = json.loads(path.read_text())
        data["process"]["started_at"] = datetime.fromtimestamp(
            self.now - 30, timezone.utc
        ).isoformat()
        path.write_text(json.dumps(data))
        self.db_health(age=60)
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("UNVERIFIED", "matrix-db-before-process")
        )

    def crash_budget(self, streak: int, *, running: bool = True, age: float = 5) -> None:
        (self.data / "crash-budget.json").write_text(json.dumps({
            "v": 1, "running": running, "started_at": self.now - age, "streak": streak,
        }))

    def test_foreign_health_pid_is_unverified(self) -> None:
        # The recorded writer is still alive (a non-bridge process here, so
        # it does not count as a second Matrix match): a second writer.
        (self.proc / "100").mkdir()
        self.process(200, channel="matrix")
        self.health(100)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid"))

    def test_dead_health_writer_is_a_restart(self) -> None:
        self.process(200, channel="matrix")
        self.health(100)
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid-restarting")
        )

    def test_single_rapid_crash_is_still_a_restart(self) -> None:
        self.process(200, channel="matrix")
        self.health(100)
        self.crash_budget(1)
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid-restarting")
        )

    def test_crash_streak_with_unreported_process_is_crash_loop(self) -> None:
        # ccc-node#2141: one node on 2026-10-01, 957 DNS crash-restarts; every watch
        # saw a fresh process that had not yet written health.json.
        self.process(200, channel="matrix")
        self.health(100, age=40)
        self.crash_budget(776)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DOWN", "crash-loop"))

    def test_run_in_back_off_delay_is_still_crash_loop(self) -> None:
        # started_at includes the back-off delay: a sleeping run is in the future.
        self.process(200, channel="matrix")
        self.health(100)
        self.crash_budget(4, age=-20)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DOWN", "crash-loop"))

    def test_stale_streak_of_a_stable_run_is_not_a_crash_loop(self) -> None:
        # A sub-alert streak survives mark_stable(); once the run has outlived
        # the crash window it is a recovered process, not a crash loop.
        self.process(200, channel="matrix")
        self.health(100)
        self.crash_budget(2, age=3600)
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid-restarting")
        )
        self.crash_budget(2, age=probe.CRASH_WINDOW_SECS)
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid-restarting")
        )

    def test_crash_loop_wins_over_live_foreign_writer(self) -> None:
        (self.proc / "100").mkdir()
        self.process(200, channel="matrix")
        self.health(100)
        self.crash_budget(3)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DOWN", "crash-loop"))

    def test_crash_streak_ignored_when_health_pid_matches(self) -> None:
        self.process(200, channel="matrix")
        self.health(200)
        self.db_health()
        self.crash_budget(5)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("OK", "db-ready"))

    def test_malformed_or_stopped_crash_budget_is_ignored(self) -> None:
        self.process(200, channel="matrix")
        self.health(100)
        for payload in ("not json", json.dumps({"running": True, "streak": True}),
                        json.dumps({"running": True, "streak": "9"}), json.dumps([1]),
                        json.dumps({"running": True, "streak": 9}),
                        json.dumps({"running": True, "streak": 9, "started_at": "x"})):
            (self.data / "crash-budget.json").write_text(payload)
            self.assertEqual(
                probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid-restarting"),
                payload,
            )
        self.crash_budget(9, running=False)
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid-restarting")
        )

    def test_degraded_health_is_alerted(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="degraded")
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DEGRADED", "health-degraded"))

    # ccc-node#2098: the health snapshot stays degraded until the next turn
    # succeeds, which on a quiet channel can be days after one provider timeout.
    def test_old_agent_error_with_ready_transport_is_ok(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="degraded", error_age=2 * 3600, ok_age=3 * 3600)
        self.db_health("ready")
        self.assertEqual(probe.probe(self.proc, now=self.now), ("OK", "degraded-stale-error"))

    def test_recent_agent_error_is_still_degraded(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="degraded", error_age=300, ok_age=3 * 3600)
        self.db_health("ready")
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DEGRADED", "health-degraded"))
        # The window is a parameter: a narrower one makes the same error stale.
        self.assertEqual(
            probe.probe(self.proc, now=self.now, max_error_age=60), ("OK", "degraded-stale-error")
        )

    def test_old_agent_error_cannot_mask_transport_retry(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="degraded", error_age=2 * 3600)
        self.db_health("network-retry")
        # Transport is not ready: the stale-error exemption does not apply and
        # the node stays DEGRADED (health verdict keeps its precedence).
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DEGRADED", "health-degraded"))

    def test_degraded_for_another_reason_stays_degraded(self) -> None:
        self.process(200, channel="matrix")
        # An old agent error exists, but the service names a different cause.
        self.health(200, state="degraded", error_age=2 * 3600, reason="Telegram: transport degraded")
        self.db_health("ready")
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DEGRADED", "health-degraded"))
        # And an agent error that is newer than the last success but has no
        # parseable timestamp is not stale either.
        self.health(200, state="degraded", error_age=2 * 3600)
        path = self.data / "health.json"
        snapshot = json.loads(path.read_text())
        snapshot["agent"]["last_error_at"] = "not-a-timestamp"
        path.write_text(json.dumps(snapshot))
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DEGRADED", "health-degraded"))

    def test_fresh_process_health_cannot_mask_transport_retry(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="available")
        self.db_health("network-retry")
        self.assertEqual(
            probe.probe(self.proc, now=self.now), ("DEGRADED", "db-network-retry")
        )

    def test_cgroup_identifies_matrix_when_channel_env_unreadable(self) -> None:
        self.process(200, channel="telegram", cgroup="/system.slice/ccc-matrix-bridge.service")
        (self.proc / "200" / "environ").unlink()
        self.assertEqual(probe.probe(self.proc), ("UNVERIFIED", "data-directory"))


if __name__ == "__main__":
    unittest.main()
