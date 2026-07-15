"""Behavior tests for BotDeliveryMixin deny/error/empty/no-match paths (#348).

Drives the real `_handle_text_message` routing (approval replies, resume
selection with provider-mismatch/invalid-number/no-match branches, pending
question answers, queue overflow) and the fail-open file-send path over a real
SessionManager/SessionStore in a temp directory. Replaces source-string-style
assertions with observed behavior.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

from telegram_bot.core.bot_delivery import BotDeliveryMixin
from telegram_bot.session.manager import SessionManager
from telegram_bot.session.store import SessionStore


class _ReplyRecorder:
    def __init__(self):
        self.texts = []

    async def __call__(self, text, *args, **kwargs):
        self.texts.append(text)


class DeliveryHarness(BotDeliveryMixin):
    """Real delivery mixin over a real session store with scripted collaborators."""

    def __init__(self, tmpdir: str, *, overflow: bool = False):
        store = SessionStore(Path(tmpdir) / "sessions.json")
        store.initialize()
        self._session_manager = SessionManager(
            store, SimpleNamespace(agent_provider="claude")
        )
        self._config = SimpleNamespace(project_root=str(tmpdir))
        self._runtime_active_sessions = set()
        self._project_chat = SimpleNamespace(
            get_session_last_assistant_message=lambda sid: f"resumed {sid}"
        )
        self._overflow = overflow
        self.access_granted = True
        self.approval_result: Optional[str] = None
        self.processed_texts = []
        self.enqueued = 0

    # -- collaborators normally provided by the composing TelegramBot --------

    async def _check_access(self, update) -> bool:
        return self.access_granted

    @staticmethod
    def _require_message(update):
        return update.message

    @staticmethod
    def _require_user(update):
        return update.effective_user

    @staticmethod
    def _require_chat(update):
        return update.effective_chat

    @staticmethod
    def _conversation_key(user_id: int, chat_id: Optional[int] = None) -> Any:
        if chat_id is None or chat_id == user_id:
            return user_id
        return f"{user_id}:{chat_id}"

    async def _resolve_codex_approval_text(self, user_id, chat_id, text):
        return self.approval_result

    def _active_provider(self) -> str:
        return "claude"

    def _own_bot_id(self):
        return None

    async def _maybe_capture_outside_approval(self, user_id, text, chat_id=None):
        return None

    async def _process_user_message_text(self, update, user_id, text):
        self.processed_texts.append(text)

    async def _enqueue_user_task(self, key, run_task, on_overflow) -> bool:
        self.enqueued += 1
        if self._overflow:
            await on_overflow()
            return False
        await run_task()
        return True


def _update(text: str, user_id: int = 1, chat_id: int = 10):
    reply = _ReplyRecorder()
    message = SimpleNamespace(
        text=text,
        reply_text=reply,
        reply_to_message=None,
        message_id=1000,
    )
    return (
        SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=chat_id),
        ),
        reply,
    )


class HandleTextMessageTests(unittest.TestCase):
    def _harness(self, **kwargs) -> DeliveryHarness:
        return DeliveryHarness(tempfile.mkdtemp(), **kwargs)

    def _seed_resume_list(self, bot: DeliveryHarness):
        asyncio.run(
            bot._session_manager.patch_session(
                "1:10",
                updates={
                    "resume_list": [
                        ["sid-claude", "claude session", "claude"],
                        ["sid-codex", "codex session", "codex"],
                    ]
                },
            )
        )

    def test_denied_access_short_circuits_everything(self):
        bot = self._harness()
        bot.access_granted = False
        update, reply = _update("hello")

        asyncio.run(bot._handle_text_message(update, None))

        self.assertEqual(reply.texts, [])
        self.assertEqual(bot.enqueued, 0)

    def test_empty_text_is_ignored(self):
        bot = self._harness()
        update, reply = _update("")

        asyncio.run(bot._handle_text_message(update, None))

        self.assertEqual(reply.texts, [])
        self.assertEqual(bot.enqueued, 0)

    def test_codex_approval_reply_answers_without_enqueueing(self):
        bot = self._harness()
        for verdict, expected in [
            ("allowed", "✅ Approved."),
            ("denied", "❌ Denied."),
            ("expired", "ℹ️ Approval expired; denied."),
            ("ambiguous", "⚠️ Multiple approvals are pending; use the buttons."),
        ]:
            with self.subTest(verdict=verdict):
                bot.approval_result = verdict
                update, reply = _update("yes")
                asyncio.run(bot._handle_text_message(update, None))
                self.assertEqual(reply.texts, [expected])
        self.assertEqual(bot.enqueued, 0)

    def test_resume_selection_switches_session(self):
        bot = self._harness()
        self._seed_resume_list(bot)
        update, reply = _update("1")

        asyncio.run(bot._handle_text_message(update, None))

        self.assertIn("✅ Switched to session: claude session", reply.texts)
        self.assertIn("📋 resumed sid-claude", reply.texts)
        session = asyncio.run(bot._session_manager.get_session("1:10"))
        self.assertEqual(session.get("session_id"), "sid-claude")
        self.assertNotIn("resume_list", session)
        self.assertIn("1:10", bot._runtime_active_sessions)

    def test_resume_selection_with_provider_mismatch_is_rejected(self):
        bot = self._harness()
        self._seed_resume_list(bot)
        update, reply = _update("2")  # codex session while claude is active

        asyncio.run(bot._handle_text_message(update, None))

        self.assertEqual(len(reply.texts), 1)
        self.assertIn("Provider mismatch", reply.texts[0])
        session = asyncio.run(bot._session_manager.get_session("1:10"))
        self.assertNotIn("session_id", session)

    def test_resume_selection_out_of_range_is_no_match(self):
        bot = self._harness()
        self._seed_resume_list(bot)
        update, reply = _update("9")

        asyncio.run(bot._handle_text_message(update, None))

        self.assertEqual(reply.texts, ["❌ Invalid number, please try again."])
        self.assertEqual(bot.enqueued, 0)

    def test_non_number_clears_resume_list_and_processes_text(self):
        bot = self._harness()
        self._seed_resume_list(bot)
        update, reply = _update("let's keep working")

        asyncio.run(bot._handle_text_message(update, None))

        session = asyncio.run(bot._session_manager.get_session("1:10"))
        self.assertNotIn("resume_list", session)
        self.assertEqual(bot.processed_texts, ["let's keep working"])

    def test_pending_question_answer_is_consumed_without_enqueueing(self):
        bot = self._harness()
        asyncio.run(
            bot._session_manager.set_pending_question(
                "1:10", "q1", {"question": "pick one"}
            )
        )
        update, reply = _update("option A")

        asyncio.run(bot._handle_text_message(update, None))

        self.assertTrue(reply.texts and reply.texts[0].startswith("✅ Answer received"))
        self.assertEqual(bot.enqueued, 0)
        self.assertIsNone(asyncio.run(bot._session_manager.get_pending_question("1:10")))

    def test_queue_overflow_replies_instead_of_processing(self):
        bot = self._harness(overflow=True)
        update, reply = _update("do more work")

        asyncio.run(bot._handle_text_message(update, None))

        self.assertEqual(len(reply.texts), 1)
        self.assertIn("Processing previous messages", reply.texts[0])
        self.assertEqual(bot.processed_texts, [])


class SendFilePathsTests(unittest.TestCase):
    def test_failed_send_is_logged_and_does_not_abort_remaining_files(self):
        tmpdir = Path(tempfile.mkdtemp())
        good = tmpdir / "b.txt"
        bad = tmpdir / "a.txt"
        for f in (bad, good):
            f.write_text("x", encoding="utf-8")

        sent = []

        class _Bot:
            async def send_document(self, chat_id, document):
                name = Path(document.name).name
                if name == "a.txt":
                    raise RuntimeError("telegram unavailable")
                sent.append(name)

            async def send_photo(self, chat_id, photo):
                raise AssertionError("no image files in this test")

        bot = DeliveryHarness(str(tmpdir))
        bot._IMAGE_EXTS = {".png"}
        bot._require_application = lambda: SimpleNamespace(bot=_Bot())

        with self.assertLogs("telegram_bot.core.bot_delivery", level="WARNING") as logs:
            asyncio.run(bot._send_file_paths(10, [bad, good]))

        self.assertEqual(sent, ["b.txt"])
        self.assertTrue(any("Failed to send file" in m for m in logs.output))


class _ResolveHarness(BotDeliveryMixin):
    """Minimal harness exercising the real _resolve_paths over on-disk files."""

    from telegram_bot.core.bot import TelegramBot as _T

    _FILE_PATH_RE = _T._FILE_PATH_RE
    _IMAGE_EXTS = _T._IMAGE_EXTS

    def __init__(self, root: Path):
        self._config = SimpleNamespace(project_root=str(root))

    def _project_root(self) -> Path:
        return Path(self._config.project_root).resolve()


class ResolvePathsExtensionTests(unittest.TestCase):
    def _resolve(self, tmpdir: Path, content: str):
        return [p.name for p in _ResolveHarness(tmpdir)._resolve_paths(content)]

    def test_deliverable_document_data_and_media_types_are_detected(self):
        tmpdir = Path(tempfile.mkdtemp())
        names = [
            "report.pdf", "data.csv", "summary.md", "notes.txt", "sheet.xlsx",
            "paper.docx", "config.json", "events.jsonl", "run.log", "logo.svg",
            "clip.mov", "voice.wav", "book.epub", "slides.pptx",
        ]
        for n in names:
            (tmpdir / n).write_text("x", encoding="utf-8")
        content = "\n".join(f"Saved to {tmpdir}/{n}" for n in names)

        resolved = self._resolve(tmpdir, content)

        self.assertEqual(sorted(resolved), sorted(names))

    def test_source_code_files_are_not_auto_sent(self):
        # Files an ordinary coding turn edits must not be pushed every reply.
        tmpdir = Path(tempfile.mkdtemp())
        for n in ("app.py", "index.js", "main.ts", "run.sh", "lib.rs"):
            (tmpdir / n).write_text("x", encoding="utf-8")
        content = "\n".join(f"Edited {tmpdir}/{n}" for n in ("app.py", "index.js", "main.ts", "run.sh", "lib.rs"))

        self.assertEqual(self._resolve(tmpdir, content), [])

    def test_json_extension_is_not_clipped_to_js(self):
        tmpdir = Path(tempfile.mkdtemp())
        (tmpdir / "config.json").write_text("{}", encoding="utf-8")

        self.assertEqual(self._resolve(tmpdir, f"see {tmpdir}/config.json"), ["config.json"])

    def test_oversize_file_is_skipped(self):
        tmpdir = Path(tempfile.mkdtemp())
        big = tmpdir / "huge.pdf"
        big.write_bytes(b"0" * 10)
        import os as _os

        _os.truncate(big, 60 * 1024 * 1024)  # 60 MB > 50 MB ceiling

        self.assertEqual(self._resolve(tmpdir, f"here {tmpdir}/huge.pdf"), [])


class MaybePromptOutsideFilesTests(unittest.TestCase):
    """Gating for offering to send files that resolved outside PROJECT_ROOT."""

    def _bot(self):
        bot = DeliveryHarness(str(Path(tempfile.mkdtemp())))
        bot._prompt_outside_file_confirmation = AsyncMock()
        return bot

    def test_prompts_when_owner_known_and_outside_paths_present(self):
        bot = self._bot()
        paths = [Path("/etc/hosts")]
        asyncio.run(bot._maybe_prompt_outside_files(5, 7, paths))
        bot._prompt_outside_file_confirmation.assert_awaited_once_with(5, 7, paths)

    def test_no_prompt_when_owner_unknown(self):
        # Callers without a resolved owner id must not expose out-of-project paths.
        bot = self._bot()
        asyncio.run(bot._maybe_prompt_outside_files(5, None, [Path("/etc/hosts")]))
        bot._prompt_outside_file_confirmation.assert_not_awaited()

    def test_no_prompt_when_no_outside_paths(self):
        bot = self._bot()
        asyncio.run(bot._maybe_prompt_outside_files(5, 7, []))
        bot._prompt_outside_file_confirmation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
