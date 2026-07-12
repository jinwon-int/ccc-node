import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Bootstrap a dummy env BEFORE importing config so the real bridge/.env token is not used.
os.environ.setdefault("PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:dummy")

import telegram_bot.core.push_notifier as pn  # noqa: E402
from telegram_bot.core.push_notifier import PushNotifier  # noqa: E402


def _cfg(**over):
    base = dict(
        push_enabled=True,
        push_spool_dir="/tmp/ccc-spool",
        push_poll_interval=3.0,
        push_max_per_minute=10,
        push_chat_id=None,
        allowed_user_ids=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


class ResolveTargetTests(unittest.TestCase):
    def test_injected_settings_override_divergent_ambient_config(self):
        injected = _cfg(
            push_enabled=False,
            push_chat_id=None,
            allowed_user_ids=[222],
            push_spool_dir="/tmp/injected-spool",
        )
        ambient = _cfg(
            push_enabled=True,
            push_chat_id=111,
            allowed_user_ids=[111],
            push_spool_dir="/tmp/ambient-spool",
        )

        with patch.object(pn, "config", ambient):
            notifier = PushNotifier(injected)

        self.assertFalse(notifier.enabled)
        self.assertEqual(notifier._resolve_target(), 222)
        self.assertEqual(notifier.spool_dir, Path("/tmp/injected-spool"))

    def test_explicit_chat_id_wins(self):
        with patch.object(pn, "config", _cfg(push_chat_id=999, allowed_user_ids=[1, 2])):
            self.assertEqual(PushNotifier()._resolve_target(), 999)

    def test_sole_allowed_user_id(self):
        with patch.object(pn, "config", _cfg(allowed_user_ids=[42])):
            self.assertEqual(PushNotifier()._resolve_target(), 42)

    def test_ambiguous_is_none(self):
        with patch.object(pn, "config", _cfg(allowed_user_ids=[1, 2])):
            self.assertIsNone(PushNotifier()._resolve_target())

    def test_no_target_is_none(self):
        with patch.object(pn, "config", _cfg()):
            self.assertIsNone(PushNotifier()._resolve_target())


class DisabledByDefaultTests(unittest.TestCase):
    def test_disabled_run_sends_nothing(self):
        with patch.object(pn, "config", _cfg(push_enabled=False, push_chat_id=7)):
            n = PushNotifier()
            app = SimpleNamespace(bot=AsyncMock())
            asyncio.run(n.run(app, asyncio.Event()))
            app.bot.send_message.assert_not_called()


class DrainTests(unittest.TestCase):
    def _write(self, spool, name, text, dedup=None):
        d = {"ts": "T", "event": "Notification", "node": "nosuk", "text": text}
        if dedup:
            d["dedup"] = dedup
        (spool / name).write_text(json.dumps(d), encoding="utf-8")

    def test_sends_to_target_and_archives(self):
        with TemporaryDirectory() as td, patch.object(pn, "config", _cfg()):
            spool = Path(td)
            sent = spool / "sent"
            sent.mkdir()
            self._write(spool, "a.json", "approval needed")
            n = PushNotifier()
            n.spool_dir = spool
            app = SimpleNamespace(bot=AsyncMock())
            asyncio.run(n._drain(app, 555, sent))
            app.bot.send_message.assert_awaited_once()
            kwargs = app.bot.send_message.await_args.kwargs
            self.assertEqual(kwargs["chat_id"], 555)
            self.assertIn("approval needed", kwargs["text"])
            self.assertFalse((spool / "a.json").exists())
            self.assertTrue((sent / "a.json").exists())

    def test_dedup_skips_duplicate(self):
        with TemporaryDirectory() as td, patch.object(pn, "config", _cfg()):
            spool = Path(td)
            sent = spool / "sent"
            sent.mkdir()
            self._write(spool, "a.json", "same", dedup="k")
            self._write(spool, "b.json", "same", dedup="k")
            n = PushNotifier()
            n.spool_dir = spool
            app = SimpleNamespace(bot=AsyncMock())
            asyncio.run(n._drain(app, 1, sent))
            self.assertEqual(app.bot.send_message.await_count, 1)

    def test_rate_limit_defers(self):
        with TemporaryDirectory() as td, patch.object(pn, "config", _cfg(push_max_per_minute=1)):
            spool = Path(td)
            sent = spool / "sent"
            sent.mkdir()
            self._write(spool, "a.json", "one", dedup="1")
            self._write(spool, "b.json", "two", dedup="2")
            n = PushNotifier()
            n.spool_dir = spool
            app = SimpleNamespace(bot=AsyncMock())
            asyncio.run(n._drain(app, 1, sent))
            self.assertEqual(app.bot.send_message.await_count, 1)
            self.assertTrue((spool / "b.json").exists())  # deferred, not lost

    def test_send_failure_keeps_file(self):
        with TemporaryDirectory() as td, patch.object(pn, "config", _cfg()):
            spool = Path(td)
            sent = spool / "sent"
            sent.mkdir()
            self._write(spool, "a.json", "x")
            n = PushNotifier()
            n.spool_dir = spool
            app = SimpleNamespace(bot=AsyncMock())
            app.bot.send_message.side_effect = RuntimeError("network")
            asyncio.run(n._drain(app, 1, sent))
            self.assertTrue((spool / "a.json").exists())  # retained for retry

    def test_malformed_is_archived_not_retried(self):
        with TemporaryDirectory() as td, patch.object(pn, "config", _cfg()):
            spool = Path(td)
            sent = spool / "sent"
            sent.mkdir()
            (spool / "bad.json").write_text("{not json", encoding="utf-8")
            n = PushNotifier()
            n.spool_dir = spool
            app = SimpleNamespace(bot=AsyncMock())
            asyncio.run(n._drain(app, 1, sent))
            app.bot.send_message.assert_not_called()
            self.assertTrue((sent / "bad.json").exists())


class FormatTests(unittest.TestCase):
    def test_format_includes_event_node_text(self):
        out = PushNotifier._format(
            {"event": "Notification", "node": "nosuk", "ts": "T", "text": "hi"}
        )
        self.assertIn("Notification", out)
        self.assertIn("nosuk", out)
        self.assertIn("hi", out)


if __name__ == "__main__":
    unittest.main()
