import unittest
from pathlib import Path

from telegram_bot.core.conversation_paths import claude_project_dir_name


class ClaudeProjectDirNameTests(unittest.TestCase):
    def test_standard_unix_path(self):
        self.assertEqual(claude_project_dir_name(Path("/home/operator")), "-home-operator")

    def test_termux_app_private_path_replaces_dot(self):
        self.assertEqual(
            claude_project_dir_name(Path("/data/data/com.termux/files/home")),
            "-data-data-com-termux-files-home",
        )

    def test_underscore_is_replaced(self):
        self.assertEqual(claude_project_dir_name(Path("/srv/project_root")), "-srv-project-root")


if __name__ == "__main__":
    unittest.main()
