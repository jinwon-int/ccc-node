"""Dependency bootstrap policy and execution parity tests (#584 P3-2).

All execution tests use local fake executables. They never invoke a real pip
install or contact a package index.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from telegram_bot.dependency_bootstrap import (
    DependencyPaths,
    InstallMode,
    dependency_fingerprint,
    install_commands,
    resolve_install_mode,
)


VALID_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"


class DependencyPolicyTests(unittest.TestCase):
    def test_mode_precedence_is_process_then_project_then_bridge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_env = root / "project.env"
            bridge_env = root / "bridge.env"
            project_env.write_text("CCC_DEPS_UNLOCKED=0\n", encoding="utf-8")
            bridge_env.write_text("CCC_DEPS_UNLOCKED=1\n", encoding="utf-8")

            self.assertEqual(
                resolve_install_mode("1", project_env, bridge_env), InstallMode.UNLOCKED
            )
            self.assertEqual(resolve_install_mode("", project_env, bridge_env), InstallMode.LOCKED)

            project_env.write_text("IGNORED=yes\n", encoding="utf-8")
            self.assertEqual(
                resolve_install_mode(None, project_env, bridge_env), InstallMode.UNLOCKED
            )

    def test_only_literal_one_unlocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for value in ("", "0", "yes", "true", "01", " 1 ", "--force-install"):
                with self.subTest(value=value):
                    self.assertEqual(
                        resolve_install_mode(value, root / "missing", root / "missing-too"),
                        InstallMode.LOCKED,
                    )

    def test_env_reader_preserves_last_assignment_export_and_inline_comment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_env = root / "project.env"
            project_env.write_text(
                "CCC_DEPS_UNLOCKED=0\n"
                " export CCC_DEPS_UNLOCKED = 1 # operator override\n",
                encoding="utf-8",
            )

            self.assertEqual(
                resolve_install_mode(None, project_env, root / "missing"),
                InstallMode.UNLOCKED,
            )

    def test_fingerprint_matches_legacy_byte_contract_and_covers_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            requirements = root / "requirements.txt"
            pyproject = root / "pyproject.toml"
            requirements.write_bytes(b"runtime\n")
            pyproject.write_bytes(b"project\n")
            paths = DependencyPaths.from_roots(root, root / "venv", root / "project.env")

            expected = hashlib.sha256()
            for payload in (b"runtime\n", b"<absent>", b"project\n"):
                expected.update(payload)
                expected.update(b"\0")
            expected.update(b"locked")

            self.assertEqual(dependency_fingerprint(paths, InstallMode.LOCKED), expected.hexdigest())
            self.assertNotEqual(
                dependency_fingerprint(paths, InstallMode.LOCKED),
                dependency_fingerprint(paths, InstallMode.UNLOCKED),
            )

    def test_locked_and_unlocked_commands_preserve_pip_contract(self):
        root = Path("/tmp/bridge root;$(touch nope)")
        paths = DependencyPaths.from_roots(root, root / "venv", root / "project.env")

        self.assertEqual(
            install_commands(paths, InstallMode.LOCKED),
            (
                (str(paths.pip), "install", "-q", "--require-hashes", "-r", str(paths.lock)),
                (str(paths.pip), "install", "-q", "--no-deps", "-e", str(root)),
            ),
        )
        self.assertEqual(
            install_commands(paths, InstallMode.UNLOCKED),
            (
                (str(paths.pip), "install", "-q", "--upgrade", "pip"),
                (str(paths.pip), "install", "-q", "-r", str(paths.requirements)),
                (str(paths.pip), "install", "-q", "-e", str(root)),
            ),
        )


class DepsInstallExecutionTests(unittest.TestCase):
    repo_root: ClassVar[Path]

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]

    def _prepare_workspace(self, tmpdir: str, *, with_lock: bool = False) -> Path:
        """Copy startup inputs into an isolated metacharacter-heavy script root."""
        script_root = Path(tmpdir) / "bridge root [x];$(touch injected)"
        script_root.mkdir(parents=True, exist_ok=True)
        for filename in (
            "start.sh",
            "dependency_bootstrap.py",
            "requirements.txt",
            "pyproject.toml",
            ".env.example",
            "CHANGELOG.md",
        ):
            shutil.copy2(self.repo_root / filename, script_root / filename)
        if with_lock:
            shutil.copy2(
                self.repo_root / "requirements.lock.txt",
                script_root / "requirements.lock.txt",
            )

        bin_dir = script_root / "venv" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (script_root / "venv" / "pyvenv.cfg").write_text(
            f"home = {Path(sys.executable).parent}\n",
            encoding="utf-8",
        )
        (bin_dir / "python").symlink_to(sys.executable)
        fake_pip = bin_dir / "pip"
        fake_pip.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "with pathlib.Path(os.environ['PIP_CALLS_LOG']).open('a', encoding='utf-8') as f:\n"
            "    f.write(repr(sys.argv[1:]) + '\\n')\n"
            "    f.write('ANDROID_API_LEVEL=' + os.environ.get('ANDROID_API_LEVEL', '') + '\\n')\n"
            "if os.environ.get('FAIL_PIP_ARG', '-e') in sys.argv:\n"
            "    raise SystemExit(19)\n",
            encoding="utf-8",
        )
        fake_pip.chmod(0o755)
        return script_root

    def _prepare_project(self, tmpdir: str, env_lines: list[str]) -> Path:
        project_root = Path(tmpdir) / "project root & data"
        bot_dir = project_root / ".telegram_bot"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / ".env").write_text(
            "\n".join([f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}", *env_lines]) + "\n",
            encoding="utf-8",
        )
        return project_root

    def _run_start(
        self,
        script_root: Path,
        project_root: Path,
        tmpdir: str,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        fake_home = Path(tmpdir) / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        pip_log = Path(tmpdir) / "pip-calls.log"
        env = dict(os.environ)
        for key in ("PROJECT_ROOT", "CCC_DEPS_UNLOCKED", "ANDROID_API_LEVEL", "TERMUX_VERSION"):
            env.pop(key, None)
        env.update(
            {
                "HOME": str(fake_home),
                "CLAUDE_CLI_PATH": "/bin/true",
                "PIP_CALLS_LOG": str(pip_log),
                "FAIL_PIP_ARG": "-e",
            }
        )
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
        calls = pip_log.read_text(encoding="utf-8") if pip_log.exists() else ""
        return result, calls

    def test_project_env_escape_hatch_selects_unlocked_and_propagates_failure(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir)
            (script_root / ".env").write_text("CCC_DEPS_UNLOCKED=0\n", encoding="utf-8")
            project_root = self._prepare_project(tmpdir, ["CCC_DEPS_UNLOCKED=1"])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy unlocked install", result.stdout)
            self.assertIn("'--upgrade', 'pip'", pip_calls)
            self.assertIn("requirements.txt", pip_calls)
            self.assertNotIn("--require-hashes", pip_calls)
            self.assertIn("Editable bridge package installation failed", result.stdout)
            self.assertFalse((Path(tmpdir) / "injected").exists())
            self.assertFalse((script_root / "injected").exists())

    def test_process_environment_survives_bridge_env_merge(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir)
            (script_root / ".env").write_text("CCC_DEPS_UNLOCKED=0\n", encoding="utf-8")
            project_root = self._prepare_project(tmpdir, [])

            result, pip_calls = self._run_start(
                script_root,
                project_root,
                tmpdir,
                extra_env={"CCC_DEPS_UNLOCKED": "1"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("legacy unlocked install", result.stdout)
            self.assertIn("'--upgrade', 'pip'", pip_calls)

    def test_locked_default_fails_closed_without_lock_and_never_calls_pip(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir)
            project_root = self._prepare_project(tmpdir, [])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            self.assertEqual(result.returncode, 1)
            self.assertIn(f"Hash lock not found: {script_root / 'requirements.lock.txt'}", result.stdout)
            self.assertIn("CCC_DEPS_UNLOCKED=1", result.stdout)
            self.assertEqual(pip_calls, "")

    def test_option_shaped_process_value_stays_locked(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir)
            project_root = self._prepare_project(tmpdir, ["CCC_DEPS_UNLOCKED=1"])

            result, pip_calls = self._run_start(
                script_root,
                project_root,
                tmpdir,
                extra_env={"CCC_DEPS_UNLOCKED": "--force-install"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Hash lock not found", result.stdout)
            self.assertNotIn("⚠️  CCC_DEPS_UNLOCKED=1", result.stdout)
            self.assertEqual(pip_calls, "")

    def test_locked_install_uses_hashes_no_deps_and_does_not_write_cache_on_failure(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=True)
            project_root = self._prepare_project(tmpdir, [])

            result, pip_calls = self._run_start(script_root, project_root, tmpdir)

            self.assertEqual(result.returncode, 1)
            self.assertIn("'--require-hashes'", pip_calls)
            self.assertIn("'--no-deps', '-e'", pip_calls)
            self.assertNotIn("'--upgrade', 'pip'", pip_calls)
            self.assertIn("Editable bridge package installation failed", result.stdout)
            self.assertFalse((script_root / "venv" / ".req_hash").exists())

    def test_each_third_party_pip_failure_is_propagated_with_legacy_message(self):
        scenarios = (
            ([], True, "--require-hashes", "Hash-locked dependency installation failed"),
            (["CCC_DEPS_UNLOCKED=1"], False, "--upgrade", "Failed to upgrade pip"),
            (["CCC_DEPS_UNLOCKED=1"], False, "-r", "Dependency installation failed"),
        )
        for env_lines, with_lock, fail_arg, message in scenarios:
            with self.subTest(fail_arg=fail_arg), tempfile.TemporaryDirectory(
                dir=self.repo_root / "tests"
            ) as tmpdir:
                script_root = self._prepare_workspace(tmpdir, with_lock=with_lock)
                project_root = self._prepare_project(tmpdir, env_lines)

                result, _ = self._run_start(
                    script_root,
                    project_root,
                    tmpdir,
                    extra_env={"FAIL_PIP_ARG": fail_arg},
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stdout)
                self.assertFalse((script_root / "venv" / ".req_hash").exists())

    def test_success_writes_cache_and_second_run_skips_pip(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir, with_lock=True)
            project_root = self._prepare_project(tmpdir, [])

            first, first_calls = self._run_start(
                script_root, project_root, tmpdir, extra_env={"FAIL_PIP_ARG": "never"}
            )
            self.assertNotEqual(first.returncode, 0)  # stops later at the fake runtime boundary
            self.assertIn("Dependencies are up to date", first.stdout)
            self.assertTrue((script_root / "venv" / ".req_hash").is_file())

            Path(tmpdir, "pip-calls.log").unlink()
            second, second_calls = self._run_start(
                script_root, project_root, tmpdir, extra_env={"FAIL_PIP_ARG": "never"}
            )
            self.assertIn("Dependencies unchanged (requirements hash match)", second.stdout)
            self.assertEqual(second_calls, "")

    def test_termux_detection_sets_api_level_for_pip_without_changing_parent(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir)
            project_root = self._prepare_project(tmpdir, ["CCC_DEPS_UNLOCKED=1"])
            fake_bin = Path(tmpdir) / "fake bin"
            fake_bin.mkdir()
            getprop = fake_bin / "getprop"
            getprop.write_text("#!/bin/sh\nprintf 'android-35-preview\\n'\n", encoding="utf-8")
            getprop.chmod(0o755)

            result, _ = self._run_start(
                script_root,
                project_root,
                tmpdir,
                extra_env={
                    "TERMUX_VERSION": "0.118",
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertIn("Android API level auto-detected: 35", result.stdout)
            self.assertIn("ANDROID_API_LEVEL=35", (Path(tmpdir) / "pip-calls.log").read_text())

    def test_operator_android_api_level_skips_getprop(self):
        with tempfile.TemporaryDirectory(dir=self.repo_root / "tests") as tmpdir:
            script_root = self._prepare_workspace(tmpdir)
            project_root = self._prepare_project(tmpdir, ["CCC_DEPS_UNLOCKED=1"])
            result, _ = self._run_start(
                script_root,
                project_root,
                tmpdir,
                extra_env={"TERMUX_VERSION": "0.118", "ANDROID_API_LEVEL": "34"},
            )
            self.assertNotIn("auto-detected", result.stdout)
            self.assertIn("ANDROID_API_LEVEL=34", (Path(tmpdir) / "pip-calls.log").read_text())


if __name__ == "__main__":
    unittest.main()
