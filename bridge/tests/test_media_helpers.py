"""Direct unit tests for the extracted pure voice/image helpers (core/media.py).

These previously lived on the TelegramBot god object. A couple were covered
indirectly by test_voice_handler / test_voice_reply_mode; the image/url helpers
and the script-counting heuristics had little direct coverage. Testing the
module functions pins the behavior independently of the bot.
"""

import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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


class DocumentHelperTest(unittest.TestCase):
    def test_display_name_is_basename_only_and_control_chars_are_removed(self):
        display = media.sanitize_document_display_name("../../private\nreport.PDF")
        self.assertNotIn("/", display)
        self.assertNotIn("\\", display)
        self.assertNotIn("\n", display)
        self.assertEqual(display, "private report.PDF")

    def test_storage_name_is_random_and_keeps_only_a_safe_extension(self):
        first = media.build_document_file_name("../../payroll.pdf", "application/pdf")
        second = media.build_document_file_name("../../payroll.pdf", "application/pdf")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("document_"))
        self.assertTrue(first.endswith(".pdf"))
        self.assertNotIn("payroll", first)
        self.assertNotIn("/", first)

    def test_known_executable_binary_is_unsupported(self):
        self.assertFalse(
            media.is_supported_document("application/x-msdownload", "payload.exe")
        )
        self.assertFalse(media.is_supported_document("application/octet-stream", "lib.so"))
        self.assertTrue(media.is_supported_document("application/pdf", "report.pdf"))
        self.assertTrue(media.is_supported_document("application/zip", "sources.zip"))

    def test_mime_extension_mismatch_and_unknown_types_are_blocked(self):
        self.assertFalse(media.is_supported_document("application/pdf", "report.txt"))
        self.assertFalse(media.is_supported_document("application/x-unknown", "report.dat"))
        self.assertTrue(media.is_supported_document("application/octet-stream", "report.pdf"))
        self.assertTrue(media.is_supported_document("application/zip", "bundle.zip"))

    def test_yaml_and_ndjson_alias_extensions_are_supported(self):
        for mime_type, file_name in (
            ("application/x-yaml", "config.yaml"),
            ("application/x-yaml", "config.yml"),
            ("text/yaml", "config.yaml"),
            ("text/yaml", "config.yml"),
            ("application/x-ndjson", "events.jsonl"),
            ("application/x-ndjson", "events.ndjson"),
        ):
            with self.subTest(mime_type=mime_type, file_name=file_name):
                self.assertTrue(media.is_supported_document(mime_type, file_name))

    def test_document_size_parser_rejects_coercion_and_negative_values(self):
        self.assertEqual(media.parse_document_size(None), 0)
        self.assertEqual(media.parse_document_size(4), 4)
        for value in (True, "4", 4.0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    media.parse_document_size(value)

    def test_prompt_includes_bounded_metadata_and_untrusted_data_warning(self):
        prompt = media.build_document_prompt(
            Path("/project/.telegram_bot/uploads/document_abc.pdf"),
            display_name="report.pdf",
            mime_type="application/pdf",
            size_bytes=123,
            caption="summarize this",
        )
        self.assertIn("Local document path:", prompt)
        self.assertIn("report.pdf", prompt)
        self.assertIn("application/pdf", prompt)
        self.assertIn("123", prompt)
        self.assertIn("summarize this", prompt)
        self.assertIn("untrusted data", prompt)

    def test_prompt_has_default_instruction_without_caption(self):
        prompt = media.build_document_prompt(
            Path("/project/.telegram_bot/uploads/document_abc.csv"),
            display_name="data.csv",
            mime_type="text/csv",
            size_bytes=7,
            caption="",
        )
        self.assertIn("Inspect the file and summarize its relevant contents", prompt)


class DocumentStorageHelperTest(unittest.TestCase):
    def test_private_directory_fd_and_file_are_exact_modes_under_permissive_umask(self):
        with TemporaryDirectory() as td:
            upload_dir = Path(td) / "uploads"
            old_umask = os.umask(0)
            try:
                directory_fd = media.open_private_document_directory(upload_dir)
                file_fd = media.open_private_document_file(
                    directory_fd, "document_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pdf"
                )
            finally:
                os.umask(old_umask)

            try:
                directory_stat = os.fstat(directory_fd)
                file_stat = os.fstat(file_fd)
                self.assertTrue(stat.S_ISDIR(directory_stat.st_mode))
                self.assertTrue(stat.S_ISREG(file_stat.st_mode))
                self.assertEqual(stat.S_IMODE(directory_stat.st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(file_stat.st_mode), 0o600)
                self.assertEqual(file_stat.st_nlink, 1)
                if hasattr(os, "getuid"):
                    self.assertEqual(directory_stat.st_uid, os.getuid())
                    self.assertEqual(file_stat.st_uid, os.getuid())
            finally:
                os.close(file_fd)
                os.close(directory_fd)

    def test_bounded_writer_rejects_before_writing_beyond_limit(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "payload"
            with path.open("w+b") as raw:
                writer = media.BoundedDocumentWriter(raw, max_bytes=5)
                self.assertEqual(writer.write(b"1234"), 4)
                with self.assertRaises(media.DocumentSizeExceeded):
                    writer.write(b"56")
                raw.flush()
            self.assertEqual(path.read_bytes(), b"1234")
            self.assertEqual(writer.bytes_written, 4)

    def test_post_open_hardlink_is_detected(self):
        with TemporaryDirectory() as td:
            upload_dir = Path(td) / "uploads"
            directory_fd = media.open_private_document_directory(upload_dir)
            name = "document_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pdf"
            file_fd = media.open_private_document_file(directory_fd, name)
            try:
                os.link(upload_dir / name, Path(td) / "hardlink.pdf")
                with self.assertRaises(PermissionError):
                    media.validate_private_document_fd(file_fd)
            finally:
                os.close(file_fd)
                os.close(directory_fd)

    def test_stale_cleanup_removes_only_regular_generated_files_without_following_symlinks(self):
        with TemporaryDirectory() as td:
            upload_dir = Path(td) / "uploads"
            upload_dir.mkdir(mode=0o700)
            stale = upload_dir / "document_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pdf"
            fresh = upload_dir / "document_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.pdf"
            unrelated = upload_dir / "operator-note.txt"
            outside = Path(td) / "outside.pdf"
            linked = upload_dir / "document_cccccccccccccccccccccccccccccccc.pdf"
            stale.write_bytes(b"stale")
            fresh.write_bytes(b"fresh")
            unrelated.write_bytes(b"keep")
            outside.write_bytes(b"outside")
            linked.symlink_to(outside)
            os.utime(stale, (0, 0))
            os.utime(fresh, (1_000, 1_000))
            os.utime(unrelated, (0, 0))

            removed = media.cleanup_stale_document_files(
                upload_dir, max_age_seconds=100, now=1_000
            )

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(linked.is_symlink())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_executable_magic_is_blocked_even_with_document_extension(self):
        self.assertTrue(media.has_blocked_executable_magic(b"MZpayload"))
        self.assertTrue(media.has_blocked_executable_magic(b"\x7fELFpayload"))
        self.assertFalse(media.has_blocked_executable_magic(b"%PDF-1.7"))


if __name__ == "__main__":
    unittest.main()
