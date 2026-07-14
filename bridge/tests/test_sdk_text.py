"""Direct unit tests for the extracted SDK-stream / text helpers (core/sdk_text.py).

_is_shutdown_signal_error / _is_retryable_sdk_error / _extract_stream_text_delta
already had indirect coverage; _format_ask_user_question and
_detect_numbered_options had none. These pin all of them at the module boundary.
The functions are also re-exported from project_chat, which test_project_chat_*
continues to import.
"""

import unittest

from telegram_bot.core import sdk_text


class ShutdownSignalTest(unittest.TestCase):
    def test_matches_signal_signatures(self):
        self.assertTrue(sdk_text._is_shutdown_signal_error("process exited with code 143"))
        self.assertTrue(sdk_text._is_shutdown_signal_error("Killed by SIGKILL"))
        self.assertTrue(sdk_text._is_shutdown_signal_error("terminated by signal"))

    def test_non_signal_is_false(self):
        self.assertFalse(sdk_text._is_shutdown_signal_error("Invalid token"))
        self.assertFalse(sdk_text._is_shutdown_signal_error(""))


class RetryableErrorTest(unittest.TestCase):
    def test_shutdown_signal_is_retryable(self):
        self.assertTrue(sdk_text._is_retryable_sdk_error(RuntimeError("exit code 143")))

    def test_permanent_errors_not_retryable(self):
        self.assertFalse(sdk_text._is_retryable_sdk_error(ValueError("bad value")))
        self.assertFalse(sdk_text._is_retryable_sdk_error(RuntimeError("Permission denied")))

    def test_transient_types_retryable(self):
        self.assertTrue(sdk_text._is_retryable_sdk_error(TimeoutError("slow")))
        self.assertTrue(sdk_text._is_retryable_sdk_error(ConnectionResetError("reset")))

    def test_transient_message_retryable(self):
        self.assertTrue(sdk_text._is_retryable_sdk_error(RuntimeError("connection refused")))

    def test_overload_messages_are_retryable(self):
        for message in (
            "Claude is overloaded",
            "API Error 529 overloaded",
            "HTTP 503 Service Unavailable",
        ):
            with self.subTest(message=message):
                self.assertTrue(sdk_text._is_retryable_sdk_error(RuntimeError(message)))
                self.assertIn(
                    "overloaded",
                    (sdk_text.describe_cancel_reason(message) or "").lower(),
                )

    def test_overload_status_codes_require_digit_boundaries(self):
        self.assertFalse(sdk_text._is_retryable_sdk_error(RuntimeError("job 1503 failed")))
        self.assertFalse(sdk_text._is_retryable_sdk_error(RuntimeError("request 5291 failed")))

    def test_permanent_bucket_precedes_overload_status(self):
        self.assertFalse(
            sdk_text._is_retryable_sdk_error(
                RuntimeError("authentication failed with HTTP 503")
            )
        )
        self.assertIn(
            "authentication",
            (sdk_text.describe_cancel_reason("authentication failed with HTTP 503") or "").lower(),
        )


class FormatAskUserQuestionTest(unittest.TestCase):
    def test_question_with_options(self):
        tool_input = {
            "questions": [
                {
                    "question": "Pick one",
                    "options": [
                        {"label": "Yes", "description": "do it"},
                        {"label": "No"},
                    ],
                }
            ]
        }
        text, images = sdk_text._format_ask_user_question(tool_input)
        self.assertEqual(images, [])
        self.assertEqual(text, "Pick one\n\n1. Yes - do it\n2. No")

    def test_empty_questions(self):
        self.assertEqual(sdk_text._format_ask_user_question({}), ("", []))

    def test_question_without_options(self):
        text, _ = sdk_text._format_ask_user_question({"questions": [{"question": "Hi?"}]})
        self.assertEqual(text, "Hi?")


class ExtractStreamTextDeltaTest(unittest.TestCase):
    def test_text_delta_extracted(self):
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}
        self.assertEqual(sdk_text._extract_stream_text_delta(event), "hello")

    def test_non_text_delta_ignored(self):
        event = {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}
        self.assertIsNone(sdk_text._extract_stream_text_delta(event))

    def test_other_event_types_ignored(self):
        self.assertIsNone(sdk_text._extract_stream_text_delta({"type": "message_start"}))
        self.assertIsNone(sdk_text._extract_stream_text_delta("not a dict"))

    def test_empty_text_is_none(self):
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ""}}
        self.assertIsNone(sdk_text._extract_stream_text_delta(event))


class DetectNumberedOptionsTest(unittest.TestCase):
    def test_two_or_more_is_true(self):
        self.assertTrue(sdk_text._detect_numbered_options("1. Apple\n2. Banana"))

    def test_single_is_false(self):
        self.assertFalse(sdk_text._detect_numbered_options("1. Only one"))

    def test_plain_text_is_false(self):
        self.assertFalse(sdk_text._detect_numbered_options("just a sentence"))


class ReExportTest(unittest.TestCase):
    def test_project_chat_reexports(self):
        from telegram_bot.core import project_chat

        self.assertIs(project_chat._is_shutdown_signal_error, sdk_text._is_shutdown_signal_error)
        self.assertIs(project_chat._detect_numbered_options, sdk_text._detect_numbered_options)
        self.assertEqual(project_chat.RESTART_INTERRUPT_NOTICE, sdk_text.RESTART_INTERRUPT_NOTICE)


if __name__ == "__main__":
    unittest.main()


class DescribeCancelReasonTest(unittest.TestCase):
    def test_usage_limit_with_reset_hint(self):
        msg = sdk_text.describe_cancel_reason(
            "You've hit your limit · resets Jul 13, 10am (Asia/Seoul)"
        )
        self.assertIsNotNone(msg)
        self.assertIn("usage limit", msg.lower())
        self.assertIn("Jul 13, 10am", msg)

    def test_usage_limit_without_reset_hint(self):
        msg = sdk_text.describe_cancel_reason("usage limit exceeded")
        self.assertIsNotNone(msg)
        self.assertIn("usage limit", msg.lower())

    def test_auth_error(self):
        msg = sdk_text.describe_cancel_reason("Failed to authenticate. API Error: 401")
        self.assertIsNotNone(msg)
        self.assertIn("authentication", msg.lower())

    def test_overloaded(self):
        self.assertIn("overloaded", (sdk_text.describe_cancel_reason("Error 529 overloaded") or "").lower())

    def test_network(self):
        self.assertIn("connection", (sdk_text.describe_cancel_reason("connection timed out") or "").lower())

    def test_unrecognised_returns_none(self):
        # A genuine /stop has no error text -> caller keeps the generic notice.
        self.assertIsNone(sdk_text.describe_cancel_reason(None))
        self.assertIsNone(sdk_text.describe_cancel_reason(""))
        self.assertIsNone(sdk_text.describe_cancel_reason("some unrelated failure"))

    def test_reset_hint_extraction(self):
        self.assertEqual(
            sdk_text._extract_reset_hint("resets Jul 13, 10am (Asia/Seoul)"),
            "Jul 13, 10am",
        )
        self.assertIsNone(sdk_text._extract_reset_hint("no hint here"))
