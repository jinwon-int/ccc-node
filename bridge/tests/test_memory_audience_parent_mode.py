"""Regression for #659.

The memory-audience guard requires the key's parent directory (`.telegram_bot`)
to be bridge-owned and mode 0700. `Path.mkdir(mode=0o700, exist_ok=True)` only
applies the mode when it *creates* the directory, so a `.telegram_bot` created
earlier under the default umask 022 (-> 0755) stayed loose and made the bridge
fail closed on every message. `load_or_create_audience_key` must self-heal a
bridge-owned parent to 0700 instead of raising.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.core.memory_audience import _KEY_BYTES, load_or_create_audience_key


@unittest.skipUnless(
    hasattr(os, "geteuid") and not sys.platform.startswith("win"),
    "POSIX ownership/permission semantics",
)
class AudienceKeyParentModeTests(unittest.TestCase):
    def _settings(self, key_path: Path) -> SimpleNamespace:
        return SimpleNamespace(bridge_memory_audience_key_path=str(key_path))

    def _mkparent(self, root: str, mode: int) -> Path:
        parent = Path(root) / "dot-telegram_bot"
        parent.mkdir()
        os.chmod(parent, mode)  # force mode regardless of the process umask
        return parent

    def test_self_heals_bridge_owned_loose_parent(self):
        with tempfile.TemporaryDirectory() as d:
            parent = self._mkparent(d, 0o755)  # legacy default-umask dir
            self.assertTrue(stat.S_IMODE(parent.stat().st_mode) & 0o077)
            key = load_or_create_audience_key(
                self._settings(parent / "memory-audience.key")
            )  # must not raise
            self.assertEqual(len(key), _KEY_BYTES)
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)  # tightened
            keyfile = parent / "memory-audience.key"
            self.assertEqual(stat.S_IMODE(keyfile.stat().st_mode), 0o600)

    def test_already_private_parent_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            parent = self._mkparent(d, 0o700)
            key = load_or_create_audience_key(
                self._settings(parent / "memory-audience.key")
            )
            self.assertEqual(len(key), _KEY_BYTES)
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)

    def test_key_is_stable_across_calls_after_self_heal(self):
        with tempfile.TemporaryDirectory() as d:
            parent = self._mkparent(d, 0o755)
            kp = parent / "memory-audience.key"
            first = load_or_create_audience_key(self._settings(kp))
            second = load_or_create_audience_key(self._settings(kp))
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
