import importlib
import os
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


class SpacingLinesConfigTests(unittest.TestCase):
    def _build_config(self, project_root: str, extra_env: dict | None = None):
        env = {
            "PROJECT_ROOT": project_root,
            "TELEGRAM_BOT_TOKEN": "123456:abc",
        }
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env, clear=True):
            sys.modules.pop("telegram_bot.utils.config", None)
            module = importlib.import_module("telegram_bot.utils.config")
            return module.Config(telegram_bot_token="123456:abc", _env_file=None)

    def test_default_spacing_is_two(self):
        # Roomy output by default: every vertical gap is two blank lines.
        with TemporaryDirectory() as td:
            cfg = self._build_config(td)
            self.assertEqual(cfg.spacing_lines, 2)

    def test_env_override_to_compact(self):
        with TemporaryDirectory() as td:
            cfg = self._build_config(td, {"CCC_TELEGRAM_SPACING_LINES": "1"})
            self.assertEqual(cfg.spacing_lines, 1)

    def test_out_of_range_env_value_is_clamped(self):
        with TemporaryDirectory() as td:
            cfg = self._build_config(td, {"CCC_TELEGRAM_SPACING_LINES": "99"})
            self.assertEqual(cfg.spacing_lines, 3)

    def test_invalid_env_value_falls_back_to_one(self):
        with TemporaryDirectory() as td:
            cfg = self._build_config(td, {"CCC_TELEGRAM_SPACING_LINES": "oops"})
            self.assertEqual(cfg.spacing_lines, 1)


if __name__ == "__main__":
    unittest.main()
