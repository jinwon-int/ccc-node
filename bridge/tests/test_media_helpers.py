"""Direct unit tests for the extracted pure voice/image helpers (core/media.py).

These previously lived on the TelegramBot god object. A couple were covered
indirectly by test_voice_handler / test_voice_reply_mode; the image/url helpers
and the script-counting heuristics had little direct coverage. Testing the
module functions pins the behavior independently of the bot.
"""

import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.core import media


class VoiceExtensionTest(unittest.TestCase):
    def test_known_mimes(self):
        self.assertEqual(media.resolve_voice_extension("audio/ogg"), "ogg")
        self.assertEqual(media.resolve_voice_extension("audio/amr"), "amr")
        self.assertEqual(media.resolve_voice_extension("audio/mpeg"), "mp3")
        self.assertEqual(media.resolve_voice_extension("audio/x-wav"), "wav")
        self.assertEqual(media.resolve_voice_extension("audio/mp4"), "m4a")

    def test_unknown_and_none_default_ogg(self):
        self.assertEqual(media.resolve_voice_extension(None), "ogg")
        self.assertEqual(media.resolve_voice_extension("application/octet-stream"), "ogg")

    def test_build_voice_file_name_shape(self):
        name = media.build_voice_file_name(user_id=42, extension="ogg")
        self.assertTrue(name.startswith("42_"))
        self.assertTrue(name.endswith(".ogg"))


class ScriptCountTest(unittest.TestCase):
    def test_count_hanzi(self):
        self.assertEqual(media.count_hanzi("中文abc"), 2)
        self.assertEqual(media.count_hanzi("no han here"), 0)

    def test_count_english_words(self):
        self.assertEqual(media.count_english_words("hello world"), 2)
        self.assertEqual(media.count_english_words("it's a test"), 3)
        self.assertEqual(media.count_english_words("中文"), 0)


class ReplyModeTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(media.normalize_reply_mode("VOICE"), "voice")
        self.assertEqual(media.normalize_reply_mode(" text "), "text")
        self.assertEqual(media.normalize_reply_mode(None), "text")
        self.assertEqual(media.normalize_reply_mode("garbage"), "text")

    def test_resolve_next_non_macos_is_text(self):
        self.assertEqual(media.resolve_next_reply_mode("voice", is_macos=False), "text")

    def test_resolve_next_macos_voice(self):
        self.assertEqual(media.resolve_next_reply_mode("voice", is_macos=True), "voice")
        self.assertEqual(media.resolve_next_reply_mode("text", is_macos=True), "text")


class DeliveryStrategyTest(unittest.TestCase):
    def test_short_is_voice_only(self):
        self.assertEqual(media.voice_delivery_strategy("短文本"), "voice_only")

    def test_medium_is_voice_and_text(self):
        self.assertEqual(media.voice_delivery_strategy("a" * 301), "voice_and_text")

    def test_long_hanzi_is_text_only(self):
        self.assertEqual(media.voice_delivery_strategy("中" * 1001), "text_only")

    def test_long_english_is_text_only(self):
        long_english = " ".join(["word"] * 1001)
        self.assertEqual(media.voice_delivery_strategy(long_english), "text_only")


class UrlTest(unittest.TestCase):
    def test_redacts_bot_token(self):
        src = "https://api.telegram.org/file/bot123456:ABCDEF/voice/file_0.ogg"
        red = media.redact_telegram_file_url(src)
        self.assertNotIn("123456:ABCDEF", red)
        self.assertIn("/bot***REDACTED***/", red)


class ImageExtensionTest(unittest.TestCase):
    def test_from_filename_suffix(self):
        self.assertEqual(media.resolve_image_extension(None, "photo.PNG"), "png")
        self.assertEqual(media.resolve_image_extension(None, "photo.jpeg"), "jpg")

    def test_from_mime(self):
        self.assertEqual(media.resolve_image_extension("image/webp"), "webp")
        self.assertEqual(media.resolve_image_extension("image/jpeg; charset=binary"), "jpg")

    def test_default_jpg(self):
        self.assertEqual(media.resolve_image_extension("application/pdf"), "jpg")
        self.assertEqual(media.resolve_image_extension(None, "doc.txt"), "jpg")

    def test_build_image_file_name_sanitizes(self):
        name = media.build_image_file_name(7, "JP G!")
        self.assertTrue(name.startswith("image_7_"))
        self.assertTrue(name.endswith(".jpg"))


class SelectInboundImageTest(unittest.TestCase):
    def test_largest_photo_wins(self):
        small = SimpleNamespace(file_size=100, width=10, height=10)
        big = SimpleNamespace(file_size=0, width=100, height=100)
        msg = SimpleNamespace(photo=[small, big], document=None)
        chosen, kind = media.select_inbound_image(msg)
        self.assertIs(chosen, big)
        self.assertEqual(kind, "photo")

    def test_image_document_fallback(self):
        doc = SimpleNamespace(mime_type="image/png")
        msg = SimpleNamespace(photo=[], document=doc)
        chosen, kind = media.select_inbound_image(msg)
        self.assertIs(chosen, doc)
        self.assertEqual(kind, "document")

    def test_non_image_document_is_none(self):
        doc = SimpleNamespace(mime_type="application/pdf")
        msg = SimpleNamespace(photo=[], document=doc)
        chosen, kind = media.select_inbound_image(msg)
        self.assertIsNone(chosen)
        self.assertEqual(kind, "none")


class ImagePromptTest(unittest.TestCase):
    def test_includes_path_and_caption(self):
        prompt = media.build_image_prompt(Path("/tmp/x.png"), "what is this?")
        self.assertIn("/tmp/x.png", prompt)
        self.assertIn("what is this?", prompt)

    def test_default_instruction_when_no_caption(self):
        prompt = media.build_image_prompt(Path("/tmp/x.png"), "")
        self.assertIn("Please describe what is in the image", prompt)


if __name__ == "__main__":
    unittest.main()
