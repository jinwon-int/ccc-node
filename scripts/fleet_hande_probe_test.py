#!/usr/bin/env python3
"""Process, health and config contract for the 「한데」 fleet probe (#2569)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_hande_probe as probe  # noqa: E402

BRIDGE, BOT = 4100, 4200


class HandeProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.fs = self.root / "fs"
        self.fs.mkdir()
        self.data = self.root / "hande"
        self.data.mkdir()
        self.config = self.root / "config.json"
        self.write_config(notice_frames=True)
        self.now = time.time()

    # -- fixtures --------------------------------------------------------------

    def write_config(self, **values: object) -> None:
        self.config.write_text(json.dumps({"rooms": ["node"], **values}))

    def bridge(self, pid: int = BRIDGE, *, channel: str = "hande") -> None:
        target = self.proc / str(pid)
        target.mkdir()
        (target / "cmdline").write_bytes(b"/venv/bin/python\0-m\0telegram_bot\0--path\0/root\0")
        (target / "environ").write_bytes(
            f"CCC_CHANNEL={channel}\0BOT_DATA_DIR={self.data}\0"
            f"CCC_HANDE_CONFIG_PATH={self.config}\0".encode()
        )
        (target / "cgroup").write_text("0::/\n")

    def bot(self) -> None:
        target = self.proc / str(BOT)
        target.mkdir()
        (target / "cmdline").write_bytes(b"/opt/native-relay-bot/bin/native-mls-bot\0http://r\0")

    def health(
        self, pid: int = BRIDGE, *, state: str = "available", transport: dict | None = None,
        age: int = 0, **extra: object,
    ) -> None:
        snapshot: dict = {
            "process": {"pid": pid},
            "service": {"state": state, "reason": extra.pop("reason", "")},
            "telegram": transport if transport is not None else {"state": "healthy"},
            **extra,
        }
        path = self.data / "health.json"
        path.write_text(json.dumps(snapshot))
        os.utime(path, (self.now - age, self.now - age))

    def run_probe(self) -> tuple[str, str]:
        return probe.probe(self.proc, fs_roots=[self.fs], markers=[], now=self.now)

    def healthy(self) -> None:
        self.bridge()
        self.bot()
        self.health()

    # -- verdicts --------------------------------------------------------------

    def test_healthy_node_is_ok(self) -> None:
        self.healthy()
        self.assertEqual(self.run_probe(), ("OK", "available"))

    def test_node_without_hande_is_retired_not_down(self) -> None:
        self.assertEqual(self.run_probe(), ("RETIRED", "not-installed"))

    def test_installed_but_not_running_is_down(self) -> None:
        # The 2026-10-27 outage shape: unit present, EnvironmentFile missing,
        # no bridge process at all while systemd keeps retrying.
        unit = self.fs / "etc/systemd/system/ccc-hande-bridge.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
        self.bot()
        self.assertEqual(self.run_probe(), ("DOWN", "no-process"))

    def test_user_unit_and_termux_markers_count_as_installed(self) -> None:
        user_cfg = self.fs / "home/alice/.config/ccc-hande/config.json"
        user_cfg.parent.mkdir(parents=True)
        user_cfg.write_text("{}")
        self.assertEqual(self.run_probe(), ("DOWN", "no-process"))
        runit = self.root / "usr/var/service/ccc-hande-bridge"
        runit.mkdir(parents=True)
        self.assertEqual(
            probe.probe(self.proc, fs_roots=[self.root / "empty"], markers=[runit], now=self.now),
            ("DOWN", "no-process"),
        )

    def test_notices_off_is_degraded(self) -> None:
        self.healthy()
        self.write_config(notice_frames=False)
        self.assertEqual(self.run_probe(), ("DEGRADED", "notices-off"))
        self.write_config()  # key absent = the bridge's default (off)
        self.assertEqual(self.run_probe(), ("DEGRADED", "notices-off"))

    def test_missing_native_bot_is_down(self) -> None:
        self.bridge()
        self.health()
        self.assertEqual(self.run_probe(), ("DOWN", "bot-no-process"))

    def test_bot_not_connected_is_degraded(self) -> None:
        self.bridge()
        self.bot()
        self.health(transport={"state": "degraded", "last_error": "hande responder socket: no bot connected"})
        self.assertEqual(self.run_probe(), ("DEGRADED", "bot-disconnected"))
        self.health(transport={"state": "degraded", "last_error": "something else"})
        self.assertEqual(self.run_probe(), ("DEGRADED", "transport-error"))

    def test_service_states(self) -> None:
        self.bridge()
        self.bot()
        self.health(state="unavailable")
        self.assertEqual(self.run_probe(), ("DOWN", "health-unavailable"))
        self.health(state="degraded")
        self.assertEqual(self.run_probe(), ("DEGRADED", "health-degraded"))
        old = datetime.fromtimestamp(self.now - 7200, timezone.utc).isoformat()
        self.health(
            state="degraded", reason="agent: provider timeout",
            agent={"last_error": "provider timeout", "last_error_at": old, "last_ok_at": None},
        )
        self.assertEqual(self.run_probe(), ("OK", "degraded-stale-error"))

    def test_untrustworthy_evidence_is_unverified(self) -> None:
        self.bridge()
        self.bot()
        self.assertEqual(self.run_probe(), ("UNVERIFIED", "health-unreadable"))
        self.health(age=600)
        self.assertEqual(self.run_probe(), ("UNVERIFIED", "health-stale"))
        self.health(pid=9999)
        self.assertEqual(self.run_probe(), ("UNVERIFIED", "health-pid-restarting"))
        self.config.write_text("not json")
        self.health()
        self.assertEqual(self.run_probe(), ("UNVERIFIED", "hande-config"))

    def test_crash_loop_is_down(self) -> None:
        self.bridge()
        self.bot()
        self.health(pid=9999)
        (self.data / "crash-budget.json").write_text(
            json.dumps({"running": True, "streak": 3, "started_at": self.now - 5})
        )
        self.assertEqual(self.run_probe(), ("DOWN", "crash-loop"))

    def test_only_hande_bridges_count(self) -> None:
        self.bridge(4300, channel="matrix")
        self.bot()
        self.assertEqual(self.run_probe(), ("RETIRED", "not-installed"))
        self.bridge(4101)
        self.bridge(4102)
        self.assertEqual(self.run_probe(), ("UNVERIFIED", "multiple-processes"))


if __name__ == "__main__":
    unittest.main()
