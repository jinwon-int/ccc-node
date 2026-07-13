"""Execution-level regression tests for start.sh dependency install modes (#349).

The CCC_DEPS_UNLOCKED escape hatch is documented in .env.example, so it must
work from the normal project config ($PROJECT_ROOT/.telegram_bot/.env) — not
only from the inherited process environment. merge_env_files() never exports
project .env keys into the shell, so start.sh has to load the key through
read_env_with_fallback explicitly (PR #431 review finding).

These tests drive the real start.sh through its foreground path with a
pre-created fake venv: bin/python is the real interpreter (the dependency
fingerprint helper needs one) and bin/pip only logs its arguments, so no
network or real installs happen. The editable install is made to fail so the
run stops deterministically right after the mode-relevant pip calls.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar


VALID_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"


class DepsInstallModeTests(unittest.TestCase):
    repo_root: ClassVar[Path]

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]

    def _prepare_workspace(self, tmpdir: str, *, with_lock: bool = False) -> Path:
        """Copy start.sh + install inputs into an isolated script root."""
        script_root = Path(tmpdir) / "bridge"
        script_root.mkdir(parents=True, exist_ok=True)
        for filename in ("start.sh", "requirements.txt", ".env.example", "CHANGELOG.md"):
            shutil.copy2(self.repo_root / filename, script_root / filename)
        if with_lock:
            shutil.copy2(
                self.repo_root / "requirements.lock.txt",
                script_root / "requirements.lock.txt",
            )

        # Pre-created venv so ensure_venv() skips real venv creation. The
        # fingerprint helper needs a working interpreter; pip is a logging
        # fake that fails the editable install to end the run deterministically.
        bin_dir = script_root / "venv" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").symlink_to(sys.executable)
        pip_log = Path(tmpdir) / "pip-calls.log"
        fake_pip = bin_dir / "pip"
        fake_pip.write_text(
            "#!/bin/sh\n"
            f'printf \'%s\\n\' "$*" >> {str(pip_log)!r}\n'
            'case " $* " in *" -e "*) exit 1 ;; esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake_pip.chmod(0o755)
        return script_root

    def _prepare_project(self, tmpdir: str, env_lines: list[str]) -> Path:
        project_root = Path(tmpdir) / "project"
        bot_dir = project_root / ".telegram_bot"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / ".env").write_text(
            "\n".join([f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}", *env_lines]) + "\n",
            encoding="utf-8",
        )
        return project_root

    def _run_start(
        self, script_root: Path, project_root: Path, tmpdir: str, extra_env: dict | None = None
    ) -> tuple[subprocess.CompletedProcess, str]:
        fake_home = Path(tmpdir) / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        for key in ("PROJECT_ROOT", "CCC_DEPS_UNLOCKED"):
            env.pop(key, None)
        env["HOME"] = str(fake_home)
        env["CLAUDE_CLI_PATH"] = "/bin/true"
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", str(script_root / "start.sh"), str(project_root)],
            cwd=script_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=120,
        )
        pip_log = Path(tmpdir) / "pip-calls.log"
        calls = pip_log.read_text(encoding="utf-8") if pip_log.exists() else ""
        return result, calls

    def test_project_env_escape_hatch_selects_unlocked_install(self):
        """CCC_DEPS_UNLOCKED=1 in $PROJECT_ROOT/.telegram_bot/.env must be honored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=False)
            project_root = self._prepare_project(tmpdir, ["CCC_DEPS_UNLOCKED=1"])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            # The unlocked branch ran: legacy pip upgrade + requirements.txt,
            # never the hash-locked install (whose lock is deliberately absent).
            self.assertIn("legacy unlocked install", result.stdout)
            self.assertNotIn("Hash lock not found", result.stdout)
            self.assertIn("--upgrade pip", pip_calls)
            self.assertIn("-r ", pip_calls)
            self.assertNotIn("--require-hashes", pip_calls)
            # The fake pip fails the editable install to end the run there.
            self.assertEqual(result.returncode, 1)
            self.assertIn("Editable bridge package installation failed", result.stdout)

    def test_locked_default_fails_loudly_without_lock_and_makes_no_pip_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=False)
            project_root = self._prepare_project(tmpdir, [])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            self.assertEqual(result.returncode, 1)
            self.assertIn("Hash lock not found", result.stdout)
            self.assertIn("CCC_DEPS_UNLOCKED=1", result.stdout)
            self.assertEqual(pip_calls, "")

    def test_locked_default_installs_lock_with_require_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=True)
            project_root = self._prepare_project(tmpdir, [])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            self.assertIn("--require-hashes", pip_calls)
            self.assertIn("requirements.lock.txt", pip_calls)
            self.assertNotIn("--upgrade pip", pip_calls)
            # First-party editable install stays outside the lock's dep graph.
            self.assertIn("--no-deps -e", pip_calls)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Editable bridge package installation failed", result.stdout)

    def test_process_environment_still_selects_unlocked_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=False)
            project_root = self._prepare_project(tmpdir, [])

            result, pip_calls = self._run_start(
                script_root, project_root, tmpdir, extra_env={"CCC_DEPS_UNLOCKED": "1"}
            )

            self.assertIn("legacy unlocked install", result.stdout)
            self.assertIn("--upgrade pip", pip_calls)
            self.assertNotIn("--require-hashes", pip_calls)

    def test_non_one_project_env_value_stays_locked(self):
        """Only the literal "1" opts out — anything else fails closed to locked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=False)
            project_root = self._prepare_project(tmpdir, ["CCC_DEPS_UNLOCKED=yes"])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            self.assertEqual(result.returncode, 1)
            self.assertIn("Hash lock not found", result.stdout)
            self.assertEqual(pip_calls, "")


if __name__ == "__main__":
    unittest.main()
