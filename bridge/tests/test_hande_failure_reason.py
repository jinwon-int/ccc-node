"""``failure_reason``: the one-line cause a failed 「한데」 turn shows (#2813)."""

from __future__ import annotations

import pytest

from telegram_bot.core.hande.failure_reason import MAX_REASON_CHARS, failure_reason

FABLE_LIMIT = (
    "You've reached your Fable limit. Switch to another model, or manage usage"
)


def test_usage_limit_is_named_with_the_original_sentence() -> None:
    assert failure_reason("error", FABLE_LIMIT) == f"사용 한도 초과 — {FABLE_LIMIT}"


@pytest.mark.parametrize(
    "detail",
    [
        "Claude AI usage limit reached|1763100000",
        "You've hit your limit · resets 3pm (Asia/Seoul)",
        "You've hit your usage limit. Upgrade to Pro",
        "Your credit balance is too low to access the Anthropic API.",
    ],
)
def test_other_limit_phrasings_are_usage_limits(detail: str) -> None:
    assert failure_reason("error", detail).startswith("사용 한도 초과 — ")


def test_cloudflare_block_page_is_a_fixed_headline() -> None:
    detail = (
        "Failed to authenticate. API Error: 403 <!DOCTYPE html>\n"
        "<title>Attention Required! | Cloudflare</title>\n<body>…</body>"
    )
    assert failure_reason("error", detail) == "Claude API 일시 차단(Cloudflare 403)"
    assert (
        failure_reason("error", "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare")
        == "Claude API 일시 차단(Cloudflare 403)"
    )


@pytest.mark.parametrize(
    "detail",
    [
        "Failed to authenticate. API Error: 401 {\"type\":\"authentication_error\"}",
        "Invalid API key · Please run /login",
        "API Error: 403 {\"type\":\"permission_error\"} Failed to authenticate.",
        "OAuth token has expired. Please obtain a new token.",
    ],
)
def test_authentication_failures(detail: str) -> None:
    assert failure_reason("error", detail) == f"인증 실패 — {detail}"


@pytest.mark.parametrize(
    "detail",
    [
        "API Error: 429 {\"type\":\"rate_limit_error\"}",
        "API Error: 529 {\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}",
        "Too many requests, slow down",
    ],
)
def test_rate_limit_and_overload(detail: str) -> None:
    assert failure_reason("error", detail) == f"요청 과다·일시 과부하 — {detail}"


def test_unknown_multi_line_text_is_one_capped_line() -> None:
    detail = "\n\n  boom:\tsomething   broke  \n" + "second line " * 50
    assert failure_reason("error", detail) == "오류 — boom: something broke"
    long = "x " * 400
    reason = failure_reason("error", long)
    assert "\n" not in reason
    assert len(reason) <= MAX_REASON_CHARS and reason.endswith("…")
    assert len(failure_reason("error", "y" * 400)) == MAX_REASON_CHARS
    assert reason.startswith("오류 — x x")


def test_control_and_ansi_characters_are_removed() -> None:
    reason = failure_reason("error", "\x1b[31mred\x1b[0m alert\x07 ‮done\x00")
    assert reason == "오류 — red alert done"
    assert all(ch.isprintable() for ch in reason)


def test_secret_looking_tokens_are_masked() -> None:
    secret = "sk-ant-api03-" + "A1b2C3d4" * 6
    bearer = "Bearer " + "q9W8e7R6t5Y4u3I2o1P0" * 2
    digest = "0123456789abcdef" * 4
    reason = failure_reason("error", f"Failed to authenticate: {secret} {bearer} {digest}")
    assert reason.startswith("인증 실패 — ")
    for leaked in (secret, "A1b2C3d4A1b2", "q9W8e7R6t5Y4", digest[:24]):
        assert leaked not in reason
    assert "[REDACTED_CREDENTIAL]" in reason
    # Words, URLs and short ids stay readable.
    url = "manage usage at https://claude.ai/settings/usage (request req_011CUabc)"
    assert failure_reason("error", url) == f"오류 — {url}"


def test_masking_happens_before_the_cap() -> None:
    secret = "sk-" + "Z9" * 40
    reason = failure_reason("error", "a" * 120 + " " + secret)
    assert "Z9Z9" not in reason


@pytest.mark.parametrize("detail", [None, "", "   \n\t", "\x1b[0m​"])
def test_no_usable_detail_falls_back_to_the_status(detail: str | None) -> None:
    assert failure_reason("error", detail) == "error"
    assert failure_reason("uncertain", detail) == "uncertain"
    assert failure_reason("", detail) == "error"
    assert failure_reason(None, detail) == "error"


@pytest.mark.parametrize("code", ["cancelled", "worker_gone", "restart", "stopped"])
def test_a_worded_cause_never_reads_as_a_machine_code(code: str) -> None:
    # The bot compares ``reason == "cancelled"``; a detail is always headlined.
    assert failure_reason("error", code) == f"오류 — {code}"
    assert failure_reason("error", code) != code
