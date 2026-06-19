import json
import os
import pty
import re
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path
from typing import ClassVar


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StartStatusTests(unittest.TestCase):
    repo_root: ClassVar[Path]
    start_script: ClassVar[Path]

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.start_script = cls.repo_root / "start.sh"

    def _run_status(
        self, project_root: Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.start_script), str(project_root), "--status"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def _run_stop(
        self, project_root: Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.start_script), str(project_root), "--stop"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def _prepare_project(self, tmpdir: str) -> Path:
        project_root = Path(tmpdir)
        bot_dir = project_root / ".telegram_bot"
        (bot_dir / "logs").mkdir(parents=True, exist_ok=True)
        return project_root

    def _write_bot_log(self, project_root: Path, *lines: str) -> Path:
        bot_log = project_root / ".telegram_bot" / "logs" / "bot.log"
        bot_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return bot_log

    def _write_health(
        self,
        project_root: Path,
        *,
        pid: int,
        updated_at: datetime | None = None,
        process_mode: str = "foreground",
        service_state: str = "available",
        service_reason: str = "",
        telegram_state: str = "healthy",
        telegram_error: str = "",
        telegram_failures: int = 0,
        claude_state: str = "healthy",
        claude_error: str = "",
    ) -> Path:
        now = updated_at or datetime.now(timezone.utc)
        health = {
            "schema_version": 1,
            "updated_at": _iso_utc(now),
            "process": {
                "pid": pid,
                "started_at": _iso_utc(now - timedelta(minutes=5)),
                "mode": process_mode,
            },
            "service": {
                "state": service_state,
                "reason": service_reason,
            },
            "telegram": {
                "state": telegram_state,
                "last_ok_at": _iso_utc(now - timedelta(seconds=10))
                if telegram_state == "healthy"
                else None,
                "last_error_at": _iso_utc(now - timedelta(seconds=5))
                if telegram_error
                else None,
                "last_error": telegram_error,
                "consecutive_failures": telegram_failures,
            },
            "claude": {
                "state": claude_state,
                "last_ok_at": _iso_utc(now - timedelta(seconds=8))
                if claude_state == "healthy"
                else None,
                "last_error_at": _iso_utc(now - timedelta(seconds=4))
                if claude_error
                else None,
                "last_error": claude_error,
            },
        }
        health_file = project_root / ".telegram_bot" / "health.json"
        health_file.write_text(
            json.dumps(health, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return health_file

    def _prepare_script_workspace(self, tmpdir: str) -> Path:
        script_root = Path(tmpdir) / "bridge"
        script_root.mkdir(parents=True, exist_ok=True)
        for filename in (
            "start.sh",
            "requirements.txt",
            ".env.example",
            "CHANGELOG.md",
        ):
            shutil.copy2(self.repo_root / filename, script_root / filename)
        return script_root / "start.sh"

    def _make_fake_python(self, bin_dir: Path) -> None:
        fake_python = bin_dir / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_python.chmod(0o755)

    def _make_fake_launchctl(self, bin_dir: Path, log_file: Path) -> None:
        fake_launchctl = bin_dir / "launchctl"
        fake_launchctl.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {str(log_file)!r}\nexit 0\n",
            encoding="utf-8",
        )
        fake_launchctl.chmod(0o755)

    def _run_interactive_start(
        self,
        start_script: Path,
        project_root: Path,
        user_input: str,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            ["bash", str(start_script), str(project_root)],
            cwd=start_script.parent,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            text=False,
            close_fds=True,
        )
        os.close(slave_fd)
        output_chunks: list[bytes] = []
        try:
            os.write(master_fd, user_input.encode("utf-8"))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    output_chunks.append(data)
                    continue
                if process.poll() is not None:
                    break
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master_fd)

        return subprocess.CompletedProcess(
            process.args,
            process.wait(),
            b"".join(output_chunks).decode("utf-8", errors="replace"),
            "",
        )

    def test_status_no_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: unavailable", result.stdout)
            self.assertIn("Process: dead (no PID file)", result.stdout)
            self.assertIn("Service: unavailable (process not running)", result.stdout)
            self.assertIn("Telegram: unavailable (process not running)", result.stdout)
            self.assertIn("Claude: unavailable (process not running)", result.stdout)

    def test_status_stale_pid_cleans_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text("999999\n", encoding="utf-8")

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: unavailable", result.stdout)
            self.assertIn("Process: dead (stale PID: 999999)", result.stdout)
            self.assertFalse(pid_file.exists(), "stale pid file should be cleaned up")

    def test_status_missing_health_file_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: degraded", result.stdout)
            self.assertIn("Service: degraded (health missing)", result.stdout)
            self.assertIn("Telegram: degraded (health missing)", result.stdout)
            self.assertIn("Claude: degraded (health missing)", result.stdout)

    def test_status_starting_from_fresh_health(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            self._write_health(
                project_root,
                pid=os.getpid(),
                service_state="starting",
                service_reason="initializing telegram polling",
                telegram_state="degraded",
                claude_state="degraded",
            )

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: starting", result.stdout)
            self.assertIn(
                "Service: starting (initializing telegram polling)", result.stdout
            )

    def test_status_available_ignores_old_log_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            self._write_bot_log(
                project_root,
                "old error: Telegram API unreachable",
                "old error: Failed to authenticate",
            )
            self._write_health(project_root, pid=os.getpid(), service_state="available")

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: available", result.stdout)
            self.assertIn("Service: available", result.stdout)
            self.assertIn("Telegram: healthy", result.stdout)
            self.assertIn("Claude: healthy", result.stdout)
            self.assertNotIn("old error", result.stdout)

    def test_status_degraded_reports_component_reasons(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            self._write_health(
                project_root,
                pid=os.getpid(),
                service_state="degraded",
                service_reason="Telegram: connection lost; Claude: auth unavailable",
                telegram_state="degraded",
                telegram_error="connection lost",
                telegram_failures=3,
                claude_state="degraded",
                claude_error="auth unavailable",
            )

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: degraded", result.stdout)
            self.assertIn(
                "Service: degraded (Telegram: connection lost; Claude: auth unavailable)",
                result.stdout,
            )
            self.assertIn("Telegram: degraded (connection lost)", result.stdout)
            self.assertIn("Claude: degraded (auth unavailable)", result.stdout)

    def test_status_stale_health_file_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            pid_file = project_root / ".telegram_bot" / "bot.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            self._write_health(
                project_root,
                pid=os.getpid(),
                updated_at=datetime.now(timezone.utc) - timedelta(seconds=151),
                service_state="available",
            )

            result = self._run_status(project_root)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot status: degraded", result.stdout)
            self.assertIn("Service: degraded (health stale:", result.stdout)
            self.assertIn("Telegram: degraded (health stale:", result.stdout)
            self.assertIn("Claude: degraded (health stale:", result.stdout)

    def test_stop_stops_supervisor_before_bot_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            bot_dir = project_root / ".telegram_bot"
            supervisor = subprocess.Popen(["sleep", "30"])
            bot = subprocess.Popen(["sleep", "30"])
            try:
                (bot_dir / "supervisor.pid").write_text(
                    f"{supervisor.pid}\n",
                    encoding="utf-8",
                )
                (bot_dir / "bot.pid").write_text(f"{bot.pid}\n", encoding="utf-8")

                result = self._run_stop(project_root)
            finally:
                if supervisor.poll() is None:
                    supervisor.terminate()
                    supervisor.wait(timeout=5)
                if bot.poll() is None:
                    bot.terminate()
                    bot.wait(timeout=5)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Stopping daemon supervisor", result.stdout)
            self.assertIn("Stopping bot process", result.stdout)
            self.assertIn("Bot stopped", result.stdout)
            self.assertFalse((bot_dir / "supervisor.pid").exists())
            self.assertFalse((bot_dir / "bot.pid").exists())

    def test_stop_boots_out_launchd_service_when_installed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            fake_bin = Path(tmpdir) / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            launchctl_log = Path(tmpdir) / "launchctl.log"
            self._make_fake_launchctl(fake_bin, launchctl_log)

            fake_home = Path(tmpdir) / "home"
            project_slug = re.sub(r"[^a-z0-9]+", "-", project_root.name.lower()).rstrip(
                "-"
            )
            plist_file = (
                fake_home
                / "Library"
                / "LaunchAgents"
                / f"com.telegram-skill-bot.{project_slug}.plist"
            )
            plist_file.parent.mkdir(parents=True, exist_ok=True)
            plist_file.write_text("<plist/>", encoding="utf-8")

            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                pid_file = project_root / ".telegram_bot" / "bot.pid"
                pid_file.write_text(f"{sleeper.pid}\n", encoding="utf-8")

                env = os.environ.copy()
                env["HOME"] = str(fake_home)
                env["PATH"] = f"{fake_bin}:{env['PATH']}"

                result = self._run_stop(project_root, env=env)
            finally:
                if sleeper.poll() is None:
                    sleeper.terminate()
                    sleeper.wait(timeout=5)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Stopping launchd service", result.stdout)
            self.assertFalse(pid_file.exists())
            self.assertIn("bootout", launchctl_log.read_text(encoding="utf-8"))

    def test_stop_preserves_foreign_token_lock_when_bot_not_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            token = "123456789:shared-token"
            (project_root / ".telegram_bot" / ".env").write_text(
                f"TELEGRAM_BOT_TOKEN={token}\n",
                encoding="utf-8",
            )

            fake_home = Path(tmpdir) / "home"
            lock_dir = fake_home / ".telegram-bot-locks"
            lock_dir.mkdir(parents=True, exist_ok=True)

            foreign = subprocess.Popen(["sleep", "30"])
            try:
                token_hash = md5(token.encode("utf-8")).hexdigest()
                lock_file = lock_dir / f"{token_hash}.pid"
                lock_file.write_text(f"{foreign.pid}\n", encoding="utf-8")

                env = os.environ.copy()
                env["HOME"] = str(fake_home)

                result = self._run_stop(project_root, env=env)
            finally:
                if foreign.poll() is None:
                    foreign.terminate()
                    foreign.wait(timeout=5)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Bot is not running", result.stdout)
            self.assertTrue(lock_file.exists())

    def test_interactive_token_entry_updates_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            start_script = self._prepare_script_workspace(tmpdir)
            fake_bin = Path(tmpdir) / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            self._make_fake_python(fake_bin)

            fake_home = Path(tmpdir) / "home"
            cache_file = fake_home / ".telegram-bot-cache" / "update_check"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("", encoding="utf-8")

            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["CLAUDE_CLI_PATH"] = "/bin/true"

            result = self._run_interactive_start(
                start_script,
                project_root,
                "123456789:ABCdefGHIjklMNOpqrsTUVwxyz\n",
                env,
            )

            env_file = project_root / ".telegram_bot" / ".env"
            env_contents = env_file.read_text(encoding="utf-8")

            self.assertEqual(result.returncode, 1)
            self.assertIn("Enter Bot Token:", result.stdout)
            self.assertIn("Token saved to", result.stdout)
            self.assertIn(
                "TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
                env_contents,
            )
            self.assertEqual(env_contents.count("TELEGRAM_BOT_TOKEN="), 1)
            self.assertNotIn(
                "TELEGRAM_BOT_TOKEN = your_bot_token_here",
                env_contents,
            )

    def test_install_generates_launchd_plist_with_environment_variables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = self._prepare_project(tmpdir)
            start_script = self._prepare_script_workspace(tmpdir)
            (project_root / ".telegram_bot" / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=123456789:token\n",
                encoding="utf-8",
            )

            fake_bin = Path(tmpdir) / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            launchctl_log = Path(tmpdir) / "launchctl.log"
            self._make_fake_launchctl(fake_bin, launchctl_log)

            fake_home = Path(tmpdir) / "home"
            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"

            result = subprocess.run(
                ["bash", str(start_script), str(project_root), "--install"],
                cwd=start_script.parent,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            project_slug = re.sub(r"[^a-z0-9]+", "-", project_root.name.lower()).rstrip(
                "-"
            )
            plist_file = (
                fake_home
                / "Library"
                / "LaunchAgents"
                / f"com.telegram-skill-bot.{project_slug}.plist"
            )
            plist_contents = plist_file.read_text(encoding="utf-8")

            self.assertEqual(result.returncode, 0)
            self.assertIn("Installed and loaded as startup service", result.stdout)
            self.assertNotIn("<string>-l</string>", plist_contents)
            self.assertIn("<key>EnvironmentVariables</key>", plist_contents)
            self.assertIn(f"<string>{env['PATH']}</string>", plist_contents)
            self.assertIn(f"<string>{env['HOME']}</string>", plist_contents)
            self.assertIn("<string>--_launchd_child</string>", plist_contents)
            self.assertIn("bootstrap", launchctl_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
