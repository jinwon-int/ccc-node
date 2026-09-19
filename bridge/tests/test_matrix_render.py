"""Matrix HTML renderer + fence-aware chunker (#1780 PR-2b)."""

from __future__ import annotations

import re

import pytest

from telegram_bot.core.matrix.render import chunk_text, render_matrix_message


# --- render_matrix_message ----------------------------------------------------


def test_plain_text_has_no_formatted_body() -> None:
    assert render_matrix_message("hello world") == ("hello world", None)
    # Multi-line plain text needs no HTML either: clients render body newlines.
    assert render_matrix_message("line one\nline two\n\npara two") == (
        "line one\nline two\n\npara two",
        None,
    )
    assert render_matrix_message("") == ("", None)
    assert render_matrix_message("   \n  ") == ("   \n  ", None)


def test_body_is_original_text_and_html_is_escaped_first() -> None:
    body, formatted = render_matrix_message("a **b** & <c>")
    assert body == "a **b** & <c>"
    assert formatted == "<p>a <strong>b</strong> &amp; &lt;c&gt;</p>"


@pytest.mark.parametrize(
    "text",
    [
        "**x** <script>alert(1)</script>",
        "**<script>alert(1)</script>**",
        "# <img src=x onerror=alert(1)>",
        "- <iframe src=//evil>",
        "> <b onmouseover=alert(1)>x</b>",
        "`<script>`",
        "```\n<script>alert(1)</script>\n```",
        '**x** [x](https://ok.example/"><script>alert(1)</script>)',
        "**x** [x](https://ok.example/)<script>alert(1)</script>",
    ],
)
def test_html_never_passes_through(text: str) -> None:
    body, formatted = render_matrix_message(text)
    assert body == text  # plain fallback is never interpreted as HTML by clients
    assert formatted is not None
    assert "<script" not in formatted
    assert "<img" not in formatted
    assert "<iframe" not in formatted
    assert "onerror" not in formatted or "&lt;img" in formatted
    assert "onmouseover" not in formatted or "&lt;b" in formatted
    # Only tags from the fixed literal set may appear.
    tags = set(re.findall(r"</?([a-z0-9]+)", formatted))
    assert tags <= {"p", "br", "strong", "em", "code", "pre", "h1", "h2", "h3", "ul", "ol", "li", "blockquote", "a"}


@pytest.mark.parametrize(
    "text",
    [
        "[click](javascript:alert(1))",
        "[click](JAVASCRIPT:alert(1))",
        "[click](data:text/html;base64,PHNjcmlwdD4=)",
        "[click](vbscript:msgbox)",
        "[click](ftp://example.com/x)",
        "[click](//example.com/x)",
        "[click](  https://example.com)",
    ],
)
def test_non_http_links_are_not_linked(text: str) -> None:
    _body, formatted = render_matrix_message("**x** " + text)
    assert formatted is not None
    assert "<a " not in formatted
    assert "javascript:" not in formatted.lower() or "&lt;" not in formatted  # stays literal text
    assert 'href="' not in formatted


def test_http_links_are_rendered_with_escaped_href() -> None:
    _body, formatted = render_matrix_message('[docs](https://ex.com/a?b=1&c="2") and [h](http://x.y/z)')
    assert formatted == (
        '<p><a href="https://ex.com/a?b=1&amp;c=&quot;2&quot;">docs</a> and '
        '<a href="http://x.y/z">h</a></p>'
    )


def test_emphasis_never_fires_inside_a_link_target() -> None:
    _body, formatted = render_matrix_message("[a](https://ex.com/_foo_/bar) _u_")
    assert formatted == '<p><a href="https://ex.com/_foo_/bar">a</a> <em>u</em></p>'


def test_plain_dangerous_text_without_markup_stays_plain() -> None:
    # No markup at all: the transport sends a bare m.text and the body is
    # shown verbatim, never parsed as HTML.
    assert render_matrix_message("<script>alert(1)</script>") == ("<script>alert(1)</script>", None)


def test_inline_markup() -> None:
    _body, formatted = render_matrix_message("**bold** *it* _under_ `code **not bold**` snake_case a*b")
    assert formatted == (
        "<p><strong>bold</strong> <em>it</em> <em>under</em> "
        "<code>code **not bold**</code> snake_case a*b</p>"
    )


def test_headings_lists_quotes_and_paragraph_breaks() -> None:
    text = (
        "# Title\n"
        "## Sub *x*\n"
        "### Third\n"
        "#### not a heading\n"
        "para one\nsame para\n"
        "\n"
        "- a\n* b\n"
        "1. one\n2) two\n"
        "> quoted\n>second\n"
        "#hashtag stays"
    )
    _body, formatted = render_matrix_message(text)
    assert formatted == (
        "<h1>Title</h1>"
        "<h2>Sub <em>x</em></h2>"
        "<h3>Third</h3>"
        "<p>#### not a heading<br>para one<br>same para</p>"
        "<ul><li>a</li><li>b</li></ul>"
        "<ol><li>one</li><li>two</li></ol>"
        "<blockquote>quoted<br>second</blockquote>"
        "<p>#hashtag stays</p>"
    )


def test_fenced_code_block_with_language_and_escaping() -> None:
    text = "before\n```python\nif a < b:\n    print('<x>')\n```\nafter"
    _body, formatted = render_matrix_message(text)
    assert formatted == (
        "<p>before</p>"
        '<pre><code class="language-python">if a &lt; b:\n    print(&#x27;&lt;x&gt;&#x27;)</code></pre>'
        "<p>after</p>"
    )


def test_fence_language_is_sanitized_and_unclosed_fence_still_renders() -> None:
    _body, formatted = render_matrix_message('**b**\n```x"><script>\ncode\n')
    # The info string failed the language grammar, so the line is not a fence
    # opener; it renders as escaped paragraph text.
    assert formatted == "<p><strong>b</strong><br>```x&quot;&gt;&lt;script&gt;<br>code</p>"
    _body, formatted = render_matrix_message("```sh\necho hi")
    assert formatted == '<pre><code class="language-sh">echo hi</code></pre>'


def test_markup_inside_fence_is_not_interpreted() -> None:
    _body, formatted = render_matrix_message("```\n**not bold** [x](https://e.com)\n# not heading\n```")
    assert formatted == "<pre><code>**not bold** [x](https://e.com)\n# not heading</code></pre>"


def test_nul_and_crlf_are_normalized() -> None:
    body, formatted = render_matrix_message("a\x00b\r\n**c**")
    assert body == "ab\n**c**"
    assert formatted == "<p>ab<br><strong>c</strong></p>"


# --- chunk_text -----------------------------------------------------------------


def _fences(chunk: str) -> int:
    return sum(1 for line in chunk.split("\n") if line.strip().startswith("```"))


def test_chunk_short_text_and_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("short") == ["short"]
    with pytest.raises(ValueError):
        chunk_text("x", limit=10)


def test_chunk_prefers_paragraph_boundaries() -> None:
    p3 = ("p3 " * 21).strip()  # 62 chars
    text = "p1\n\np2\n\n" + p3 + "\n\np4"
    assert chunk_text(text, limit=66) == ["p1\n\np2", p3 + "\n\np4"]  # p3+p4 fit together
    chunks = chunk_text(text, limit=64)
    assert chunks == ["p1\n\np2", p3, "p4"]
    assert all(len(c) <= 64 for c in chunks)


def test_chunk_falls_back_to_line_boundaries() -> None:
    lines = [f"line {i:02d} " + "x" * 20 for i in range(12)]
    chunks = chunk_text("\n".join(lines), limit=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    # No line was cut: every chunk is a set of whole lines.
    rejoined = "\n".join(chunks).split("\n")
    assert rejoined == lines


def test_chunk_hard_cuts_a_single_oversized_line() -> None:
    chunks = chunk_text("a" * 250, limit=100)
    assert chunks == ["a" * 100, "a" * 100, "a" * 50]


def test_chunk_never_splits_inside_a_fence() -> None:
    code_lines = [f"line {i} " + "x" * 20 for i in range(40)]
    text = "intro\n\n```py\n" + "\n".join(code_lines) + "\n```\nafter"
    chunks = chunk_text(text, limit=300)
    assert all(len(c) <= 300 for c in chunks)
    assert chunks[0] == "intro"
    assert chunks[-1] == "after"
    middle = chunks[1:-1]
    assert len(middle) >= 3
    for chunk in middle:
        assert chunk.startswith("```py\n"), chunk[:20]
        assert chunk.endswith("\n```"), chunk[-20:]
        assert _fences(chunk) == 2
    recovered = [
        line for chunk in middle for line in chunk.split("\n")[1:-1]
    ]
    assert recovered == code_lines


def test_chunk_moves_a_whole_fence_to_the_next_chunk_when_it_fits() -> None:
    text = "para " * 10 + "\n\n```\n" + "\n".join(["code"] * 6) + "\n```"
    chunks = chunk_text(text, limit=80)
    assert chunks[0] == ("para " * 10)
    assert chunks[1] == "```\n" + "\n".join(["code"] * 6) + "\n```"


def test_chunk_hard_cut_inside_fence_keeps_fences_balanced() -> None:
    text = "```\n" + "y" * 150 + "\n```"
    chunks = chunk_text(text, limit=64)
    assert all(len(c) <= 64 for c in chunks)
    for chunk in chunks:
        assert chunk.startswith("```\n") and chunk.endswith("\n```")
        assert _fences(chunk) == 2
    assert "".join(c.split("\n")[1] for c in chunks) == "y" * 150


def test_chunk_unclosed_fence_is_closed_at_the_end() -> None:
    chunks = chunk_text("```\n" + "\n".join(["z" * 30] * 6), limit=100)
    assert all(_fences(c) == 2 for c in chunks)
    assert chunks[-1].endswith("\n```")


# --- chunk_text byte budgeting (#1828) ------------------------------------------


def _b(text: str) -> int:
    return len(text.encode("utf-8"))


def test_chunk_limit_counts_utf8_bytes_not_characters() -> None:
    # 12,000 Korean characters are 36,000 bytes. Budgeting in characters used to
    # emit this as ONE event, overrunning the homeserver's 65,536-byte PDU limit
    # once body + formatted_body were both attached.
    text = "가" * 12_000
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(_b(c) <= 12_000 for c in chunks)
    # ASCII is unaffected: one byte per character, so the old bound still holds.
    assert chunk_text("a" * 12_000) == ["a" * 12_000]


def test_chunk_byte_limit_is_respected_for_mixed_width_text() -> None:
    text = "\n\n".join(["한국어 문단입니다. " * 30, "ascii paragraph " * 40] * 12)
    chunks = chunk_text(text, limit=2_000)
    assert all(_b(c) <= 2_000 for c in chunks)


def test_chunk_hard_cut_never_splits_a_multibyte_character() -> None:
    for char in ("가", "漢", "🙂", "é"):
        chunks = chunk_text(char * 4_001, limit=64)
        assert all(_b(c) <= 64 for c in chunks)
        # A split code point would decode to U+FFFD (or fail outright).
        assert all("�" not in c for c in chunks)
        # Nothing is dropped and nothing is duplicated.
        assert "".join(chunks) == char * 4_001


def test_chunk_multibyte_fence_body_stays_balanced_within_byte_limit() -> None:
    # The fence opener is always ASCII (_FENCE_OPEN_RE only accepts ASCII
    # language tags), but the fenced body is not: the close-fence reserve has to
    # be measured against a byte budget or a Korean code block overruns it.
    chunks = chunk_text("```python\n" + "# 한글 주석 라인\n" * 200 + "```", limit=200)
    assert all(_b(c) <= 200 for c in chunks)
    assert all(_fences(c) == 2 for c in chunks)
