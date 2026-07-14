"""Single-sourced crash/rapid-restart policy (#445).

The bridge's two nested restart guards (in-process polling rebuild + process
supervisor) must read one canonical policy so tuning one can't silently diverge
from the other. These tests pin: the canonical file drives both layers, the
runtime numbers are unchanged (option 1 — single-source without behavior
change), and the in-process give-up → supervisor re-count contract is wired and
documented.
"""

import importlib
import os
import sys
import types
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if "telegram_bot" not in sys.modules:
    _pkg = types.ModuleType("telegram_bot")
    _pkg.__path__ = [str(BRIDGE_DIR)]
    sys.modules["telegram_bot"] = _pkg

from telegram_bot.core import crash_policy  # noqa: E402

POLICY_FILE = BRIDGE_DIR / "crash-policy.env"
LIFECYCLE = BRIDGE_DIR / "core" / "bot_lifecycle.py"
START_SH = BRIDGE_DIR / "start.sh"


class CrashPolicySingleSourceTests(unittest.TestCase):
    def test_policy_file_defines_every_key(self):
        parsed = crash_policy.parse_env_file(POLICY_FILE)
        for key in crash_policy._DEFAULTS:
            self.assertIn(key, parsed, key)

    def test_fallback_defaults_mirror_the_file(self):
        # The in-code fallback must not drift from the canonical file.
        parsed = crash_policy.parse_env_file(POLICY_FILE)
        for key, default in crash_policy._DEFAULTS.items():
            self.assertEqual(int(parsed[key]), default, key)

    def test_module_resolves_from_the_file(self):
        parsed = crash_policy.parse_env_file(POLICY_FILE)
        self.assertEqual(
            crash_policy.MAX_RAPID_CRASHES, int(parsed["CCC_MAX_RAPID_CRASHES"])
        )
        self.assertEqual(
            crash_policy.INPROCESS_MIN_UPTIME_SECONDS,
            int(parsed["CCC_INPROCESS_MIN_UPTIME_SECONDS"]),
        )
        self.assertEqual(
            crash_policy.PROCESS_CRASH_WINDOW_SECONDS,
            int(parsed["CCC_PROCESS_CRASH_WINDOW_SECONDS"]),
        )

    def test_runtime_values_unchanged(self):
        # Option 1 (#445): single-source WITHOUT changing behavior.
        self.assertEqual(crash_policy.MAX_RAPID_CRASHES, 5)
        self.assertEqual(crash_policy.INPROCESS_MIN_UPTIME_SECONDS, 30)
        self.assertEqual(crash_policy.PROCESS_CRASH_WINDOW_SECONDS, 60)

    def test_environment_overrides_file(self):
        # The supervisor exports these; a bash-launched child must match them.
        os.environ["CCC_MAX_RAPID_CRASHES"] = "9"
        try:
            reloaded = importlib.reload(crash_policy)
            self.assertEqual(reloaded.MAX_RAPID_CRASHES, 9)
        finally:
            os.environ.pop("CCC_MAX_RAPID_CRASHES", None)
            importlib.reload(crash_policy)

    def test_malformed_value_falls_back_to_default(self):
        os.environ["CCC_MAX_RAPID_CRASHES"] = "not-a-number"
        try:
            reloaded = importlib.reload(crash_policy)
            self.assertEqual(
                reloaded.MAX_RAPID_CRASHES,
                crash_policy._DEFAULTS["CCC_MAX_RAPID_CRASHES"],
            )
        finally:
            os.environ.pop("CCC_MAX_RAPID_CRASHES", None)
            importlib.reload(crash_policy)


class CrashPolicyWiringTests(unittest.TestCase):
    """Both layers read the shared policy; the escalation contract is explicit."""

    def test_in_process_guard_reads_shared_policy(self):
        text = LIFECYCLE.read_text(encoding="utf-8")
        self.assertIn("_MIN_UPTIME = crash_policy.INPROCESS_MIN_UPTIME_SECONDS", text)
        self.assertIn("_MAX_RAPID_CRASHES = crash_policy.MAX_RAPID_CRASHES", text)

    def test_supervisor_sources_shared_policy(self):
        text = START_SH.read_text(encoding="utf-8")
        self.assertIn('. "$SCRIPT_DIR/crash-policy.env"', text)
        self.assertIn('RAPID_CRASH_WINDOW="$CCC_PROCESS_CRASH_WINDOW_SECONDS"', text)
        self.assertIn('MAX_RAPID_CRASHES="$CCC_MAX_RAPID_CRASHES"', text)

    def test_in_process_give_up_documents_escalation(self):
        text = LIFECYCLE.read_text(encoding="utf-8")
        self.assertIn("Deliberate escalation to layer 2", text)
        idx = text.index("Deliberate escalation to layer 2")
        # …immediately followed by the non-zero SystemExit that ends the process.
        self.assertIn("raise SystemExit", text[idx : idx + 500])

    def test_supervisor_recounts_any_nonzero_exit(self):
        # Intent (#445): the in-process give-up (a non-zero SystemExit) is counted
        # by the supervisor as exactly ONE process crash — the crash branch keys
        # off zero vs non-zero exit, with NO special-casing of the give-up.
        text = START_SH.read_text(encoding="utf-8")
        self.assertIn('if [ "$exit_code" -eq 0 ]; then', text)
        self.assertIn("rapid_crash_count=$((rapid_crash_count + 1))", text)
        # The supervisor never parses the python give-up message.
        self.assertNotIn("Giving up", text)


if __name__ == "__main__":
    unittest.main()
