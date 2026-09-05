"""Transcript-history accessors over the single shared JSONL parser (#456).

project_chat_history.py is stdlib-only, so these run without the Claude SDK.
They pin the four accessors' return contracts on top of the shared
``iter_transcript_messages`` generator, including the differing content-extraction
rules (last-block vs first-non-empty vs first-block-with-'<'-filter), the revert
view's file-line index, and the now-uniform skip-malformed-line behavior.
"""

import json
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
    read_last_assistant_text,
)


class _CountingHandle:
    """Binary file proxy that counts the bytes actually read."""

    def __init__(self, handle):
        self._handle = handle
        self.bytes_read = 0
        self.reads = 0

    def seek(self, *args):
        return self._handle.seek(*args)

    def read(self, size=-1):
        data = self._handle.read(size)
        self.bytes_read += len(data)
        self.reads += 1
        return data


def _forward_last_assistant_text(path: Path):
    """The pre-#1479 full-scan rule, kept as the oracle for the tail read."""
    last_text = None
    for _idx, _role, content, _ts in iter_transcript_messages(path, types=("assistant",)):
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    last_text = text
    return last_text


def _assistant_line(text: str, *, extra_blocks=()) -> str:
    blocks = list(extra_blocks) + [{"type": "text", "text": text}]
    return json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": blocks}}
    )


def _user_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


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

    # -- #1479: tail-first last-assistant read ------------------------------

    def test_tail_read_on_multi_mb_transcript_reads_a_few_blocks_only(self):
        # ~4 MB transcript: 4000 user/assistant pairs with 500-byte payloads,
        # ending in a user line and an assistant tool_use-only record so the
        # scan has to skip past two non-matching records first.
        lines = []
        for i in range(4000):
            lines.append(_user_line(f"q{i} " + "u" * 500))
            lines.append(_assistant_line(f"a{i} " + "x" * 500))
        lines.append(_assistant_line("", extra_blocks=[{"type": "tool_use", "id": "t"}]))
        lines.append(_user_line("trailing user"))
        path = self._write("big", lines)
        size = path.stat().st_size
        self.assertGreater(size, 3 * 1024 * 1024)

        with open(path, "rb") as raw:
            handle = _CountingHandle(raw)
            got = read_last_assistant_text(handle)

        self.assertEqual(got, "a3999 " + "x" * 500)
        self.assertEqual(got, _forward_last_assistant_text(path))
        # One 64 KiB block suffices; the full scan would have read every byte.
        self.assertEqual(handle.reads, 1)
        self.assertLessEqual(handle.bytes_read, 64 * 1024)
        self.assertLess(handle.bytes_read * 20, size)
        # The mixin accessor is the same read plus truncation.
        self.assertEqual(
            self.host.get_session_last_assistant_message("big", max_chars=5), "a3999..."
        )

    def test_tail_read_carries_partial_lines_across_block_boundaries(self):
        # Records longer than one block must be re-assembled from the carry;
        # malformed trailing lines and a trailing newline-less line are skipped.
        long_text = "L" * 5000
        lines = [
            _assistant_line("early"),
            _assistant_line(long_text),
            "BROKEN {{{",
            _user_line("u" * 3000),
        ]
        path = self._write("span", lines)
        for block in (7, 64, 1024, 4096, 1 << 20):
            with open(path, "rb") as raw:
                self.assertEqual(
                    read_last_assistant_text(raw, block_bytes=block), long_text, block
                )
        # No trailing newline at all.
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        with open(path, "rb") as raw:
            self.assertEqual(read_last_assistant_text(raw, block_bytes=64), long_text)

    def test_tail_read_matches_forward_scan_on_edge_shapes(self):
        cases = {
            "rich": self._RICH,
            "only_users": [_user_line("a"), _user_line("b")],
            "string_content": [
                '{"type":"assistant","message":{"role":"assistant","content":"plain"}}'
            ],
            "empty_then_text": [
                _assistant_line("kept"),
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"  "}]}}',
            ],
            "role_mismatch_last": [
                _assistant_line("real"),
                '{"type":"assistant","message":{"role":"user","content":[{"type":"text","text":"nope"}]}}',
            ],
            "last_block_wins": [
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"first"},{"type":"text","text":"second"}]}}'
            ],
            "empty_file": [],
        }
        for name, lines in cases.items():
            path = self.dir / f"{name}.jsonl"
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            expected = _forward_last_assistant_text(path)
            with open(path, "rb") as raw:
                self.assertEqual(read_last_assistant_text(raw, block_bytes=16), expected, name)
            self.assertEqual(
                self.host.get_session_last_assistant_message(name), expected, name
            )

    def test_clean_response_strips_ansi_and_control_chars_via_module_regex(self):
        """#1479: the ANSI pattern is compiled once at import, not per call."""
        from telegram_bot.core import project_chat_history

        self.assertIs(
            project_chat_history._ANSI_ESCAPE_RE,
            project_chat_history._ANSI_ESCAPE_RE,
        )
        raw = "\x1b[31mred\x1b[0m\x1b]0;title\x07 text\x00\tkeep\n"
        cleaned = self.host._clean_response(raw)
        self.assertEqual(cleaned, "red0;title text\tkeep")
        self.assertNotIn("\x1b", cleaned)


if __name__ == "__main__":
    unittest.main()
