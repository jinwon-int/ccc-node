"""Tests for utils.tg_md — Markdown -> Telegram MarkdownV2 rendering."""

import pytest

from telegram_bot.utils import tg_md

_HAS_LIB = tg_md.available()
needs_lib = pytest.mark.skipif(not _HAS_LIB, reason="telegramify-markdown not installed")


def test_empty_passthrough():
    assert tg_md.to_markdownv2("") == ""


def test_available_is_bool():
    assert isinstance(tg_md.available(), bool)


def test_utf16_len_basic():
    assert tg_md.utf16_len("abc") == 3
    # astral chars (emoji) count as 2 UTF-16 code units
    assert tg_md.utf16_len("\U0001F310") == 2


def test_split_naive_fallback_when_unavailable(monkeypatch):
    # Force the library-less path and confirm naive splitting still bounds size.
    monkeypatch.setattr(tg_md, "available", lambda: False)
    big = "x" * 5000
    # split_markdownv2 imports the lib internally; simulate absence via builtins
    parts = tg_md.split_markdownv2(big, limit=4096)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == big


@needs_lib
def test_special_chars_escaped():
    out = tg_md.to_markdownv2("a_b 1+1=2 price $0.00 dot. bang!")
    # MarkdownV2 reserved chars must be backslash-escaped
    assert r"\_" in out
    assert r"\+" in out or r"\=" in out
    assert r"\." in out
    assert r"\!" in out


@needs_lib
def test_table_becomes_code_block():
    md = (
        "| node | state |\n"
        "|------|-------|\n"
        "| nosuk | active |\n"
        "| soonwook | active |\n"
    )
    out = tg_md.to_markdownv2(md)
    # tables render inside a fenced code block (aligned, monospace)
    assert "```" in out
    assert "nosuk" in out and "soonwook" in out


@needs_lib
def test_heading_emoji_stripped():
    out = tg_md.to_markdownv2("# Title\n\nbody")
    # decorative heading emojis are stripped; structure kept via bold
    assert "📌" not in out and "✏" not in out
    assert "Title" in out


@needs_lib
def test_readable_spacing_gap_survives_conversion():
    # The readable renderer widens gaps with an invisible NBSP filler line
    # precisely because this converter collapses runs of truly blank lines.
    # Guard the contract end-to-end: a spacing=2 gap must still be visible
    # (one blank line + one NBSP line) after MarkdownV2 conversion.
    from telegram_bot.utils.tg_readable import GAP_FILLER_LINE, to_readable

    out = tg_md.to_markdownv2(to_readable("para one\n\npara two", spacing=2))
    assert f"\n\n{GAP_FILLER_LINE}\n" in out
    # Loose list items keep their filler line too (the converter would render
    # a plain loose list with no blank between items at all).
    out = tg_md.to_markdownv2(
        to_readable("- item one\n- item two", loose=True, spacing=2)
    )
    assert GAP_FILLER_LINE in out


def _visual_gap(out: str, a_frag: str, b_frag: str) -> int:
    """Count visually blank lines between the lines containing the fragments."""
    lines = out.split("\n")
    ai = None
    for i, line in enumerate(lines):
        if ai is None and a_frag in line:
            ai = i
        elif ai is not None and b_frag in line:
            return sum(1 for gap_line in lines[ai + 1 : i] if gap_line.strip() == "")
    raise AssertionError(f"fragments not found in order: {a_frag!r}, {b_frag!r}")


@needs_lib
def test_rendered_gaps_are_uniform_across_boundary_types():
    # The converter eats the blank line after list items but keeps it after
    # paragraphs; the readable renderer compensates per boundary so the gap
    # the user SEES is uniform: spacing between blocks, spacing-1 between list
    # items, exactly one blank line under a heading.
    from telegram_bot.utils.tg_readable import to_readable

    doc = (
        "## Title\n\nintro para\n\n- item one\n- item two\n\n"
        "closing para\n\nsecond para"
    )
    out = tg_md.to_markdownv2(to_readable(doc, loose=True, spacing=2))
    assert _visual_gap(out, "Title", "intro") == 1  # under heading
    assert _visual_gap(out, "intro", "item one") == 2  # block gap
    assert _visual_gap(out, "item one", "item two") == 1  # list items
    assert _visual_gap(out, "item two", "closing") == 2  # list end -> block
    assert _visual_gap(out, "closing", "second") == 2  # block gap


@needs_lib
def test_long_content_splits_within_limit():
    md = "\n\n".join(f"paragraph {i} with text" for i in range(800))
    out = tg_md.to_markdownv2(md)
    parts = tg_md.split_markdownv2(out, limit=4096)
    assert len(parts) >= 1
    assert all(tg_md.utf16_len(p) <= 4096 for p in parts)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
