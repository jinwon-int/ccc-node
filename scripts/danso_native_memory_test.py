#!/usr/bin/env python3
"""Unit tests for the danso native-memory switch (danso #52 §9 bridge
transition): the flag gate, the CLI argument shape, and the loader skip.

Run directly (prints a PASS=<n> FAIL=<n> tally).
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

_WORK = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "danso_memory_under_test", _WORK / "bridge" / "core" / "danso_memory.py"
)
danso_memory = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(danso_memory)


class NativeMemorySwitchTest(unittest.TestCase):
    def test_default_is_off(self):
        settings = SimpleNamespace(danso_native_memory="off")
        self.assertFalse(danso_memory.native_memory_enabled(settings))

    def test_settings_attribute_enables(self):
        settings = SimpleNamespace(danso_native_memory="native-read")
        self.assertTrue(danso_memory.native_memory_enabled(settings))

    def test_environment_overrides_settings(self):
        settings = SimpleNamespace(danso_native_memory="off")
        os.environ["CCC_DANSO_NATIVE_MEMORY"] = "1"
        try:
            self.assertTrue(danso_memory.native_memory_enabled(settings))
        finally:
            os.environ["CCC_DANSO_NATIVE_MEMORY"] = "0"
        self.assertFalse(danso_memory.native_memory_enabled(settings))
        os.environ.pop("CCC_DANSO_NATIVE_MEMORY", None)

    def test_command_args_shape(self):
        settings = SimpleNamespace()
        audience = SimpleNamespace(
            kind="private",
            scope="private-" + "a" * 32,
            root=Path("/memory-root"),
        )
        args = danso_memory.native_memory_command_args(settings, audience)
        self.assertEqual(
            args,
            ["--memory", "read", "--memory-dir", "/memory-root",
             "--memory-scope", "private-" + "a" * 32],
        )


if __name__ == "__main__":
    import sys
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeMemorySwitchTest)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    print(f"PASS={result.testsRun - len(result.failures) - len(result.errors)} "
          f"FAIL={len(result.failures) + len(result.errors)}")
    sys.exit(0 if result.wasSuccessful() else 1)
