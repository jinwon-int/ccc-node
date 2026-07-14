"""Transcript-history accessors over the single shared JSONL parser (#456).

project_chat_history.py is stdlib-only, so these run without the Claude SDK.
They pin the four accessors' return contracts on top of the shared
``iter_transcript_messages`` generator, including the differing content-extraction
rules (last-block vs first-non-empty vs first-block-with-'<'-filter), the revert
view's file-line index, and the now-uniform skip-malformed-line behavior.
"""

import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if "telegram_bot" not in sys.modules:
    _pkg = types.ModuleType("telegram_bot")
    _pkg.__path__ = [str(BRIDGE_DIR)]
    sys.modules["telegram_bot"] = _pkg

from telegram_bot.core.project_chat_history import (  # noqa: E402
    ProjectChatHistoryMixin,
    iter_transcript_messages,
)


class _Host(ProjectChatHistoryMixin):
    def __init__(self, conversations_dir: Path):
        self.conversations_dir = conversations_dir


class TranscriptHistoryTests(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        self.host = _Host(self.dir)

    def _write(self, session_id: str, lines) -> Path:
        p = self.dir / f"{session_id}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    _RICH = [
        '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"first real question"}]},"timestamp":"t1"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"assistant one"}]},"timestamp":"t2"}',
        "THIS IS A MALFORMED LINE {{{",
        '{"type":"user","message":{"role":"user","content":[{"type":"text","text":""},{"type":"text","text":"second nonempty"}]},"timestamp":"t4"}',
        '{"type":"system","message":{"role":"system","content":"ignore"},"timestamp":"t5"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"assistant TWO last"}]},"timestamp":"t6"}',
        '{"type":"user","message":{"role":"user","content":"third as string"},"timestamp":"t7"}',
    ]

    def test_missing_file_returns_empty(self):
        self.assertIsNone(self.host.get_session_last_assistant_message("nope"))
        self.assertEqual(self.host.get_recent_messages("nope"), [])
        self.assertEqual(self.host.get_conversation_history("nope"), [])
        self.assertEqual(list(iter_transcript_messages(self.dir / "nope.jsonl")), [])

    def test_last_assistant_message_is_the_final_block(self):
        self._write("s", self._RICH)
        self.assertEqual(
            self.host.get_session_last_assistant_message("s"), "assistant TWO last"
        )

    def test_last_assistant_message_truncates(self):
        self._write(
            "s",
            [
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"%s"}]}}'
                % ("x" * 50)
            ],
        )
        got = self.host.get_session_last_assistant_message("s", max_chars=10)
        self.assertEqual(got, "x" * 10 + "...")

    def test_recent_messages_both_roles_first_nonempty_block(self):
        self._write("s", self._RICH)
        got = self.host.get_recent_messages("s", limit=10)
        self.assertEqual(
            [(m["role"], m["content"]) for m in got],
            [
                ("user", "first real question"),
                ("assistant", "assistant one"),
                ("user", "second nonempty"),  # first EMPTY block skipped
                ("assistant", "assistant TWO last"),
                ("user", "third as string"),  # string content
            ],
        )
        # malformed + system lines are excluded
        self.assertNotIn("ignore", [m["content"] for m in got])

    def test_recent_messages_limit_keeps_chronological_tail(self):
        self._write("s", self._RICH)
        got = self.host.get_recent_messages("s", limit=2)
        self.assertEqual(
            [m["content"] for m in got], ["assistant TWO last", "third as string"]
        )

    def test_conversation_history_users_only_reversed_with_file_index(self):
        self._write("s", self._RICH)
        got = self.host.get_conversation_history("s", limit=10)
        # user-only, newest-first, index is the 0-based FILE line position.
        self.assertEqual(
            [(m["index"], m["content"]) for m in got],
            [(6, "third as string"), (3, "second nonempty"), (0, "first real question")],
        )

    def test_first_user_message_filters_tag_and_truncates(self):
        self._write(
            "s",
            [
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"a"}]}}',
                '{"type":"user","message":{"role":"user","content":"<command>hidden</command>"}}',
                '{"type":"user","message":{"role":"user","content":"%s"}}' % ("y" * 100),
            ],
        )
        got = ProjectChatHistoryMixin._extract_first_user_message(self.dir / "s.jsonl")
        self.assertEqual(got, "y" * 80)  # '<'-tag line skipped, truncated to 80

    def test_malformed_lines_are_skipped_uniformly(self):
        # #456: the first-user accessor used to abort on a malformed first line;
        # now every accessor skips malformed lines (parse loop is single-sourced).
        self._write(
            "s",
            [
                "BROKEN {{{",
                '{"type":"user","message":{"role":"user","content":"real first"}}',
            ],
        )
        self.assertEqual(
            ProjectChatHistoryMixin._extract_first_user_message(self.dir / "s.jsonl"),
            "real first",
        )

    def test_iter_transcript_messages_type_filter_and_role_match(self):
        self._write(
            "s",
            [
                '{"type":"user","message":{"role":"user","content":"u"},"timestamp":"a"}',
                '{"type":"assistant","message":{"role":"assistant","content":"x"}}',
                '{"type":"user","message":{"role":"assistant","content":"mismatch"}}',
            ],
        )
        users = list(iter_transcript_messages(self.dir / "s.jsonl", types=("user",)))
        # role must match type: the type=user/role=assistant record is excluded.
        self.assertEqual([(i, r, c, t) for i, r, c, t in users], [(0, "user", "u", "a")])

    def test_list_sessions_uses_first_user_preview(self):
        self._write("alpha", self._RICH)
        sessions = self.host.list_sessions(limit=5)
        self.assertEqual(len(sessions), 1)
        sid, preview, _mtime = sessions[0]
        self.assertEqual(sid, "alpha")
        self.assertEqual(preview, "first real question")


if __name__ == "__main__":
    unittest.main()
