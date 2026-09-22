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

    def health(self, pid: int, state: str = "available", *, age: int = 0) -> None:
        path = self.data / "health.json"
        path.write_text(json.dumps({
            "process": {
                "pid": pid,
                "started_at": datetime.fromtimestamp(
                    self.now - 600, timezone.utc
                ).isoformat(),
            },
            "service": {"state": state},
        }))
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
        self.assertEqual(probe.probe(self.proc, now=self.now), ("OK", "available"))

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

    def test_foreign_health_pid_is_unverified(self) -> None:
        self.process(200, channel="matrix")
        self.health(100)
        self.assertEqual(probe.probe(self.proc, now=self.now), ("UNVERIFIED", "health-pid"))

    def test_degraded_health_is_alerted(self) -> None:
        self.process(200, channel="matrix")
        self.health(200, state="degraded")
        self.assertEqual(probe.probe(self.proc, now=self.now), ("DEGRADED", "health-degraded"))

    def test_cgroup_identifies_matrix_when_channel_env_unreadable(self) -> None:
        self.process(200, channel="telegram", cgroup="/system.slice/ccc-matrix-bridge.service")
        (self.proc / "200" / "environ").unlink()
        self.assertEqual(probe.probe(self.proc), ("UNVERIFIED", "data-directory"))


if __name__ == "__main__":
    unittest.main()
