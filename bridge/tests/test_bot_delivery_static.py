"""Static guards on the non-streaming delivery path in ``core/bot.py``.

Regression context: live streaming is opt-in (default off via
``CCC_TELEGRAM_STREAMING``), so normal replies go through
``TelegramBot._deliver_markdown`` rather than the streaming finalize path. The
readable renderer (loose-spacing etc.) was only wired into the streaming path,
so once streaming defaulted off every reply skipped it and lost its mobile
readability formatting. These static checks assert the renderer stays wired
into the non-streaming path so the regression cannot silently return.

``core/bot.py`` cannot be imported in the CI test environment (it eagerly
constructs config-backed singletons that need a live PROJECT_ROOT), so we assert
on the source text — the same approach used by ``test_start_script_static.py``.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOT_PY = ROOT / "core" / "bot.py"


def _bot_text() -> str:
    return BOT_PY.read_text(encoding="utf-8")


class DeliverMarkdownRendererWiringTests(unittest.TestCase):
    def test_bot_imports_tg_readable(self):
        self.assertIn("from telegram_bot.utils import tg_readable", _bot_text())

    def test_deliver_markdown_applies_readable_renderer_before_markdownv2(self):
        text = _bot_text()
        start = text.index("async def _deliver_markdown")
        end = text.index("async def ", start + 1)
        body = text[start:end]

        # The renderer is applied via the shared helper...
        self.assertIn("tg_readable.render_for_delivery(", body)
        # ...gated on the readable-renderer / loose-spacing config flags...
        self.assertIn("enable_readable_renderer", body)
        self.assertIn("enable_loose_spacing", body)

        # ...and its output (render_text) is what gets converted to MarkdownV2,
        # not the raw content (the bug was passing raw content straight through).
        self.assertIn("render_text = tg_readable.render_for_delivery(", body)
        self.assertIn("tg_md.to_markdownv2(render_text)", body)
        self.assertNotIn("tg_md.to_markdownv2(content)", body)

        render = body.index("render_text = tg_readable.render_for_delivery(")
        convert = body.index("tg_md.to_markdownv2(render_text)")
        self.assertLess(render, convert)


if __name__ == "__main__":
    unittest.main()
