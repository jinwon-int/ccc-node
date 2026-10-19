"""Inherited-channel guard for start.sh lifecycle actions (#2177).

A provider shell spawned by the Matrix frontend inherits CCC_CHANNEL=matrix.
Running the Telegram restart command from that shell made start.sh act on the
Matrix channel: it stopped the live Matrix frontend and launched a second
Matrix frontend (a Termux node, 2026-10-08). start.sh manages only the
Telegram bridge, so start/stop/restart that resolve to matrix -- inherited or
via --channel matrix -- exit 10, and --channel telegram overrides the
inherited channel.

The Matrix frontend is a decoy process whose cmdline matches
`python -m telegram_bot --path <root>` and whose environ carries
CCC_CHANNEL=matrix -- exactly what start.sh's process match reads.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MATRIX_SCOPED = {
    "CCC_CHANNEL": "matrix",
    "SESSION_STORE_PATH": "/nonexistent/.ccc-matrix/sessions.json",
    "CCC_MATRIX_CONFIG_PATH": "/nonexistent/family-matrix/config.json",
}
# The hande frontend (#2380) is the same kind of thing: its own unit, its own
# data dir, never started through start.sh.
HANDE_SCOPED = {
    "CCC_CHANNEL": "hande",
    "SESSION_STORE_PATH": "/nonexistent/.ccc-hande/sessions.json",
    "CCC_HANDE_CONFIG_PATH": "/nonexistent/ccc-hande/config.json",
}


@unittest.skipUnless(sys.platform.startswith("linux"), "uses /proc + pgrep")
@unittest.skipIf(shutil.which("pgrep") is None, "pgrep unavailable")
class InheritedChannelGuardTests(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[1]
        self.start_script = self.repo_root / "start.sh"
        self.root = str(Path(tempfile.mkdtemp(prefix="ccc-chan-guard-")).resolve())
        self._procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self._procs:
            p.kill()
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _clean_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if key in ("CCC_CHANNEL", "PROJECT_ROOT", "CCC_AGENT_PROVIDER", "BOT_DATA_DIR",
                       "LOGS_DIR", "SESSION_STORE_PATH", "CCC_BOT_ENV_FILE") or key.startswith(("CCC_MATRIX_", "CCC_HANDE_")):
                env.pop(key)
        return env

    def _telegram_bridge(self) -> subprocess.Popen:
        return self._decoy({})

    def _matrix_frontend(self) -> subprocess.Popen:
        return self._decoy(MATRIX_SCOPED)

    def _decoy(self, extra_env: dict[str, str], cwd: str | None = None) -> subprocess.Popen:
        env = self._clean_env()
        env.update(extra_env)
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             "-m", "telegram_bot", "--path", self.root],
            env=env,
            cwd=cwd,
        )
        self._procs.append(p)
        time.sleep(0.5)
        return p

    def _start_sh(
        self, *args: str, inherit_matrix: bool, inherit: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        env = self._clean_env()
        if inherit_matrix:
            env.update(MATRIX_SCOPED)
        if inherit:
            env.update(inherit)
        return subprocess.run(
            ["bash", str(self.start_script), "--path", self.root, *args],
            cwd=self.repo_root, text=True, capture_output=True, check=False,
            env=env, timeout=120,
        )

    def test_stop_from_matrix_shell_is_refused_and_matrix_survives(self):
        matrix = self._matrix_frontend()
        r = self._start_sh("--stop", inherit_matrix=True)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("target-channel=matrix", r.stdout)
        self.assertIn("--channel telegram", r.stdout)
        self.assertNotIn("--channel matrix", r.stdout)
        time.sleep(0.3)
        self.assertIsNone(matrix.poll(), "Matrix frontend was stopped by a Telegram --stop")

    def test_restart_from_matrix_shell_is_refused_before_stop(self):
        matrix = self._matrix_frontend()
        r = self._start_sh("--restart", "-d", inherit_matrix=True)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertNotIn("Stopping", r.stdout)
        time.sleep(0.3)
        self.assertIsNone(matrix.poll(), "Matrix frontend was stopped by a Telegram --restart")

    def test_daemon_start_from_matrix_shell_is_refused(self):
        r = self._start_sh("--daemon", inherit_matrix=True)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("action=run", r.stdout)

    def test_explicit_telegram_channel_acts_on_telegram_only(self):
        # The intended operation from a Matrix shell: --channel telegram stops
        # the Telegram bridge and leaves the Matrix frontend alone.
        telegram = self._telegram_bridge()
        matrix = self._matrix_frontend()
        r = self._start_sh("--stop", "--channel", "telegram", inherit_matrix=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("inherited matrix -> requested telegram", r.stdout)
        time.sleep(0.5)
        self.assertIsNotNone(telegram.poll(), "--channel telegram --stop left the Telegram bridge running")
        self.assertIsNone(matrix.poll(), "--channel telegram --stop killed the Matrix frontend")

    def test_explicit_matrix_channel_is_refused_and_telegram_survives(self):
        # start.sh's pid/supervisor/token files are Telegram's whatever the
        # channel, so "--channel matrix --stop" used to stop Telegram too.
        telegram = self._telegram_bridge()
        matrix = self._matrix_frontend()
        for inherit in (True, False):
            r = self._start_sh("--stop", "--channel", "matrix", inherit_matrix=inherit)
            self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
            self.assertIn("(--channel matrix)", r.stdout)
            self.assertNotIn("--channel matrix if", r.stdout)
        time.sleep(0.3)
        self.assertIsNone(telegram.poll(), "--channel matrix --stop killed the Telegram bridge")
        self.assertIsNone(matrix.poll(), "--channel matrix --stop killed the Matrix frontend")

    def test_provider_child_shell_of_matrix_frontend_targets_telegram(self):
        # #2177 proposal 1: the bridge now blanks the selection keys in its
        # provider children (BOT_DATA_DIR stays for the memory hooks). A
        # Telegram --stop typed in that tool shell needs no --channel and
        # never reaches the Matrix frontend.
        from telegram_bot.utils.channel_environment import channel_selection_blank_overlay

        telegram = self._telegram_bridge()
        matrix = self._matrix_frontend()
        frontend = {**MATRIX_SCOPED, "BOT_DATA_DIR": "/nonexistent/.ccc-matrix"}
        env = self._clean_env()
        env.update(frontend)
        env.update(channel_selection_blank_overlay(frontend))
        r = subprocess.run(
            ["bash", str(self.start_script), "--path", self.root, "--stop"],
            cwd=self.repo_root, text=True, capture_output=True, check=False,
            env=env, timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Refused: action=", r.stdout)
        time.sleep(0.5)
        self.assertIsNotNone(telegram.poll(), "--stop from a provider child left Telegram running")
        self.assertIsNone(matrix.poll(), "--stop from a provider child killed the Matrix frontend")

    def test_status_is_not_guarded(self):
        r = self._start_sh("--status", inherit_matrix=True)
        self.assertNotEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertNotIn("Refused: action=", r.stdout)

    # -- --status for the Matrix channel (#2312) ------------------------------
    # start.sh's HEALTH_FILE is always <root>/.telegram_bot/health.json, so
    # `--channel matrix --status` used to render the Telegram bridge's idle
    # occupancy while a Matrix turn ran.

    @staticmethod
    def _write_health(data_dir: Path, pid: int, occupancy: str) -> None:
        now = datetime.now(timezone.utc)
        stamp = now.isoformat().replace("+00:00", "Z")
        occ: dict = {"state": occupancy, "observed_at": stamp}
        if occupancy == "occupied":
            occ["oldest_turn_started_at"] = (now - timedelta(minutes=3)).isoformat().replace(
                "+00:00", "Z"
            )
            occ["elapsed_seconds"] = 180
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "health.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "updated_at": stamp,
                    "process": {"pid": pid},
                    "service": {"state": "available", "reason": ""},
                    "telegram": {"state": "healthy", "last_error": ""},
                    "agent": {"state": "healthy", "provider": "claude", "last_error": ""},
                    "workload": {
                        "active_requests": 1 if occupancy == "occupied" else 0,
                        "oldest_request_age_seconds": 180 if occupancy == "occupied" else 0,
                        "waiting_for_turn": 0,
                        "turn_occupancy": occ,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _telegram_idle(self) -> subprocess.Popen:
        telegram = self._telegram_bridge()
        self._write_health(Path(self.root) / ".telegram_bot", telegram.pid, "idle")
        return telegram

    def test_matrix_status_renders_the_frontend_health_not_telegram(self):
        self._telegram_idle()
        data_dir = Path(self.root) / ".ccc-matrix"
        matrix = self._decoy({**MATRIX_SCOPED, "BOT_DATA_DIR": str(data_dir)})
        self._write_health(data_dir, matrix.pid, "occupied")

        for label, args, inherit in (
            ("--channel matrix", ("--status", "--channel", "matrix"), False),
            ("inherited CCC_CHANNEL=matrix", ("--status",), True),
        ):
            with self.subTest(label):
                r = self._start_sh(*args, inherit_matrix=inherit)
                out = r.stdout + r.stderr
                self.assertEqual(r.returncode, 0, out)
                self.assertIn(f"health: {data_dir}/health.json", r.stdout)
                self.assertIn(f"Process: alive (PID: {matrix.pid})", r.stdout)
                self.assertIn("Turn occupancy: occupied (1 active turn;", r.stdout)
                self.assertIn("   Matrix: healthy", r.stdout)
                self.assertNotIn("   Telegram:", r.stdout)
        self.assertIsNone(matrix.poll(), "--status must not touch the Matrix frontend")

    def test_matrix_status_resolves_a_relative_data_dir_against_the_frontend_cwd(self):
        matrix = self._decoy(
            {**MATRIX_SCOPED, "BOT_DATA_DIR": ".ccc-matrix"}, cwd=self.root
        )
        self._write_health(Path(self.root) / ".ccc-matrix", matrix.pid, "occupied")
        r = self._start_sh("--status", "--channel", "matrix", inherit_matrix=False)
        self.assertIn("Turn occupancy: occupied", r.stdout, r.stdout + r.stderr)

    def test_matrix_status_without_frontend_is_unavailable_not_telegram(self):
        self._telegram_idle()
        r = self._start_sh("--status", "--channel", "matrix", inherit_matrix=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Bot status: unavailable", r.stdout)
        self.assertIn("no Matrix frontend for this --path", r.stdout)
        self.assertNotIn("Turn occupancy: idle", r.stdout)

    def test_matrix_status_without_data_dir_never_falls_back_to_telegram(self):
        self._telegram_idle()
        matrix = self._matrix_frontend()  # no BOT_DATA_DIR in its environment
        r = self._start_sh("--status", "--channel", "matrix", inherit_matrix=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Bot status: degraded", r.stdout)
        self.assertIn(f"PID: {matrix.pid}", r.stdout)
        self.assertIn("Matrix data dir unknown", r.stdout)
        self.assertNotIn("Turn occupancy: idle", r.stdout)

    def test_telegram_status_ignores_a_matrix_frontend(self):
        telegram = self._telegram_idle()
        (Path(self.root) / ".telegram_bot" / "bot.pid").write_text(str(telegram.pid))
        data_dir = Path(self.root) / ".ccc-matrix"
        matrix = self._decoy({**MATRIX_SCOPED, "BOT_DATA_DIR": str(data_dir)})
        self._write_health(data_dir, matrix.pid, "occupied")
        r = self._start_sh("--status", inherit_matrix=False)
        self.assertIn(f"Process: alive (PID: {telegram.pid})", r.stdout, r.stdout + r.stderr)
        self.assertIn("Turn occupancy: idle", r.stdout)
        self.assertIn("   Telegram: healthy", r.stdout)
        self.assertNotIn("Channel: matrix", r.stdout)

    # -- the hande frontend follows the Matrix rules (#2380) ------------------

    def test_stop_from_hande_shell_is_refused_and_hande_survives(self):
        hande = self._decoy(HANDE_SCOPED)
        r = self._start_sh("--stop", inherit_matrix=False, inherit=HANDE_SCOPED)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("target-channel=hande (inherited CCC_CHANNEL=hande)", r.stdout)
        self.assertIn("systemctl restart ccc-hande-bridge", r.stdout)
        time.sleep(0.3)
        self.assertIsNone(hande.poll(), "hande frontend was stopped by a Telegram --stop")

    def test_explicit_hande_channel_is_refused_and_telegram_survives(self):
        telegram = self._telegram_bridge()
        r = self._start_sh("--stop", "--channel", "hande", inherit_matrix=False)
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("(--channel hande)", r.stdout)
        time.sleep(0.3)
        self.assertIsNone(telegram.poll(), "--channel hande --stop killed the Telegram bridge")

    def test_explicit_telegram_from_hande_shell_drops_hande_settings(self):
        telegram = self._telegram_bridge()
        hande = self._decoy(HANDE_SCOPED)
        r = self._start_sh("--stop", "--channel", "telegram", inherit_matrix=False, inherit=HANDE_SCOPED)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("inherited hande -> requested telegram", r.stdout)
        time.sleep(0.5)
        self.assertIsNotNone(telegram.poll(), "--channel telegram --stop left the Telegram bridge running")
        self.assertIsNone(hande.poll(), "--channel telegram --stop killed the hande frontend")

    def test_hande_status_renders_the_frontend_health_with_its_own_label(self):
        self._telegram_idle()
        data_dir = Path(self.root) / ".ccc-hande"
        hande = self._decoy({**HANDE_SCOPED, "BOT_DATA_DIR": str(data_dir)})
        self._write_health(data_dir, hande.pid, "occupied")
        r = self._start_sh("--status", "--channel", "hande", inherit_matrix=False)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("Channel: hande (health: " + str(data_dir) + "/health.json)", r.stdout)
        self.assertIn(f"Process: alive (PID: {hande.pid})", r.stdout)
        self.assertIn("Turn occupancy: occupied (1 active turn;", r.stdout)
        self.assertIn("   Hande: healthy", r.stdout)
        self.assertNotIn("   Telegram:", r.stdout)
        hande.kill()
        hande.wait(timeout=5)
        time.sleep(0.3)
        r = self._start_sh("--status", "--channel", "hande", inherit_matrix=False)
        self.assertIn("no Hande frontend for this --path", r.stdout, r.stdout + r.stderr)
        self.assertIn("ccc-hande-bridge", r.stdout)

    def test_telegram_shell_is_unaffected(self):
        r = self._start_sh("--stop", inherit_matrix=False)
        self.assertNotEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertNotIn("Refused: action=", r.stdout)

    def test_invalid_channel_value_is_rejected(self):
        r = self._start_sh("--stop", "--channel", "slack", inherit_matrix=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--channel must be telegram, matrix or hande", r.stderr)


class RestartSpawnCarriesChannelTests(unittest.TestCase):
    def test_restart_spawn_args_forward_requested_channel(self):
        # The restart driver re-invokes this start.sh to start the new bridge;
        # it must forward --channel or the child would hit the guard (exit 7).
        src = (Path(__file__).resolve().parents[1] / "start.sh").read_text()
        self.assertIn('[ -n "$REQUESTED_CHANNEL" ] && spawn_args+=("--channel" "$REQUESTED_CHANNEL")', src)


if __name__ == "__main__":
    unittest.main()
