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

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MATRIX_SCOPED = {
    "CCC_CHANNEL": "matrix",
    "SESSION_STORE_PATH": "/nonexistent/.ccc-matrix/sessions.json",
    "CCC_MATRIX_CONFIG_PATH": "/nonexistent/family-matrix/config.json",
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
                       "LOGS_DIR", "SESSION_STORE_PATH", "CCC_BOT_ENV_FILE") or key.startswith("CCC_MATRIX_"):
                env.pop(key)
        return env

    def _telegram_bridge(self) -> subprocess.Popen:
        return self._decoy({})

    def _matrix_frontend(self) -> subprocess.Popen:
        return self._decoy(MATRIX_SCOPED)

    def _decoy(self, extra_env: dict[str, str]) -> subprocess.Popen:
        env = self._clean_env()
        env.update(extra_env)
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             "-m", "telegram_bot", "--path", self.root],
            env=env,
        )
        self._procs.append(p)
        time.sleep(0.5)
        return p

    def _start_sh(self, *args: str, inherit_matrix: bool) -> subprocess.CompletedProcess:
        env = self._clean_env()
        if inherit_matrix:
            env.update(MATRIX_SCOPED)
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

    def test_telegram_shell_is_unaffected(self):
        r = self._start_sh("--stop", inherit_matrix=False)
        self.assertNotEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertNotIn("Refused: action=", r.stdout)

    def test_invalid_channel_value_is_rejected(self):
        r = self._start_sh("--stop", "--channel", "slack", inherit_matrix=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--channel must be telegram or matrix", r.stderr)


class RestartSpawnCarriesChannelTests(unittest.TestCase):
    def test_restart_spawn_args_forward_requested_channel(self):
        # The restart driver re-invokes this start.sh to start the new bridge;
        # it must forward --channel or the child would hit the guard (exit 7).
        src = (Path(__file__).resolve().parents[1] / "start.sh").read_text()
        self.assertIn('[ -n "$REQUESTED_CHANNEL" ] && spawn_args+=("--channel" "$REQUESTED_CHANNEL")', src)


if __name__ == "__main__":
    unittest.main()
