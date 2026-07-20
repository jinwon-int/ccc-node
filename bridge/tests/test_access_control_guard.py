"""Tests for the fail-closed access-control startup guard.

The bridge must refuse to start when ALLOWED_USER_IDS is empty (which would
otherwise open it to every Telegram user), unless CCC_REQUIRE_ALLOWLIST=false
is set to intentionally run an open bridge.
"""

# ruff: noqa: E402
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# config.py reads PROJECT_ROOT (and a bot token) at import time; set them before
# importing the bot module so collection works without a configured environment.
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")

import unittest
from types import SimpleNamespace

from telegram_bot.core.bot import enforce_access_control


def _cfg(
    require_allowlist=True,
    allowed_user_ids=None,
    execution_profile="strict-project",
):
    return SimpleNamespace(
        require_allowlist=require_allowlist,
        allowed_user_ids=allowed_user_ids or [],
        execution_profile=execution_profile,
    )


class AccessControlGuardTests(unittest.TestCase):
    def test_empty_allowlist_with_guard_refuses_start(self):
        with self.assertRaises(SystemExit):
            enforce_access_control(_cfg(require_allowlist=True, allowed_user_ids=[]))

    def test_populated_allowlist_starts(self):
        # Should not raise.
        enforce_access_control(_cfg(require_allowlist=True, allowed_user_ids=[42]))

    def test_open_bridge_opt_out_starts(self):
        # Operator explicitly opted into an open bridge.
        enforce_access_control(_cfg(require_allowlist=False, allowed_user_ids=[]))

    def test_default_require_allowlist_is_fail_closed(self):
        # A config object missing the attribute still defaults to fail-closed.
        with self.assertRaises(SystemExit):
            enforce_access_control(SimpleNamespace(allowed_user_ids=[]))

    def test_owner_operator_refuses_open_or_multi_owner_bridge(self):
        unsafe = (
            _cfg(False, [], "owner-operator"),
            _cfg(True, [42, 43], "owner-operator"),
            _cfg(True, [42, 43], " OWNER_OPERATOR "),
        )
        for cfg in unsafe:
            with self.subTest(cfg=cfg), self.assertRaises(SystemExit):
                enforce_access_control(cfg)

    def test_owner_operator_starts_with_one_required_owner(self):
        enforce_access_control(_cfg(True, [42], "owner-operator"))
        enforce_access_control(_cfg(True, [42, 42], " owner_operator "))

    def test_real_entrypoint_logs_effective_policy_before_unsafe_owner_exit(self):
        repo_root = Path(__file__).resolve().parents[2]
        bridge_dir = repo_root / "bridge"
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            project_root.chmod(0o700)
            data_dir = project_root / ".telegram_bot"
            data_dir.mkdir(mode=0o700)
            env_file = data_dir / ".env"
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=123456:test\n"
                "ALLOWED_USER_IDS=42\n"
                "CCC_REQUIRE_ALLOWLIST=false\n"
                "CCC_BRIDGE_EXECUTION_PROFILE=owner-operator\n"
                "CCC_BRIDGE_BASH_POLICY=auto-approve\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)

            env = os.environ.copy()
            for key in (
                "PROJECT_ROOT",
                "TELEGRAM_BOT_TOKEN",
                "ALLOWED_USER_IDS",
                "CCC_REQUIRE_ALLOWLIST",
                "CCC_BRIDGE_EXECUTION_PROFILE",
                "CCC_BRIDGE_BASH_POLICY",
                "CCC_BOT_ENV_FILE",
            ):
                env.pop(key, None)
            env["BOT_DEBUG"] = "1"
            env["PYTHONPATH"] = str(repo_root / ".github" / "pythonpath")
            # Isolate from the node's real package fallback bridge/.env (a live
            # node keeps a populated one with ALLOWED_USER_IDS and a real token).
            env["CCC_BOT_ENV_FILE"] = str(project_root / "missing-package.env")

            result = subprocess.run(
                [sys.executable, "-m", "telegram_bot", "--path", str(project_root), "--debug"],
                cwd=bridge_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            log_text = (data_dir / "logs" / "bot.log").read_text(encoding="utf-8")
            self.assertIn(
                "bridge_execution_policy execution_profile=disabled "
                "bash_policy=disabled host_scope=false",
                log_text,
            )
            self.assertIn("Refusing to start owner-operator execution", log_text)
            self.assertNotIn("123456:test", log_text)
            self.assertNotIn("ALLOWED_USER_IDS=42", log_text)
            self.assertFalse((data_dir / "health.json").exists())

    def test_real_entrypoint_logs_bound_project_bash_policy_before_allowlist_exit(self):
        repo_root = Path(__file__).resolve().parents[2]
        bridge_dir = repo_root / "bridge"
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            project_root.chmod(0o700)
            data_dir = project_root / ".telegram_bot"
            data_dir.mkdir(mode=0o700)
            env_file = data_dir / ".env"
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=123456:test\n"
                "CCC_REQUIRE_ALLOWLIST=true\n"
                "CCC_BRIDGE_EXECUTION_PROFILE=strict-project\n"
                "CCC_BRIDGE_BASH_POLICY=approve-each\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)

            env = os.environ.copy()
            for key in (
                "PROJECT_ROOT",
                "TELEGRAM_BOT_TOKEN",
                "ALLOWED_USER_IDS",
                "CCC_REQUIRE_ALLOWLIST",
                "CCC_BRIDGE_EXECUTION_PROFILE",
                "CCC_BRIDGE_BASH_POLICY",
                "CCC_BOT_ENV_FILE",
            ):
                env.pop(key, None)
            env["BOT_DEBUG"] = "1"
            env["PYTHONPATH"] = str(repo_root / ".github" / "pythonpath")
            # Isolate from the node's real package fallback bridge/.env (a live
            # node keeps a populated one with ALLOWED_USER_IDS and a real token,
            # which would defeat the empty-allowlist refusal under test).
            env["CCC_BOT_ENV_FILE"] = str(project_root / "missing-package.env")

            result = subprocess.run(
                [sys.executable, "-m", "telegram_bot", "--path", str(project_root), "--debug"],
                cwd=bridge_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            log_text = (data_dir / "logs" / "bot.log").read_text(encoding="utf-8")
            self.assertIn(
                "bridge_execution_policy execution_profile=strict-project "
                "bash_policy=approve-each host_scope=false",
                log_text,
            )
            self.assertIn("Refusing to start: ALLOWED_USER_IDS is empty", log_text)
            self.assertFalse((data_dir / "health.json").exists())


if __name__ == "__main__":
    unittest.main()
