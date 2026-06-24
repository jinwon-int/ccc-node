"""Tests for the fail-closed access-control startup guard.

The bridge must refuse to start when ALLOWED_USER_IDS is empty (which would
otherwise open it to every Telegram user), unless CCC_REQUIRE_ALLOWLIST=false
is set to intentionally run an open bridge.
"""

# ruff: noqa: E402
import os
from pathlib import Path

# config.py reads PROJECT_ROOT (and a bot token) at import time; set them before
# importing the bot module so collection works without a configured environment.
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")

import unittest
from types import SimpleNamespace

from telegram_bot.core.bot import enforce_access_control


def _cfg(require_allowlist=True, allowed_user_ids=None):
    return SimpleNamespace(
        require_allowlist=require_allowlist,
        allowed_user_ids=allowed_user_ids or [],
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


if __name__ == "__main__":
    unittest.main()
