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
def test_long_content_splits_within_limit():
    md = "\n\n".join(f"paragraph {i} with text" for i in range(800))
    out = tg_md.to_markdownv2(md)
    parts = tg_md.split_markdownv2(out, limit=4096)
    assert len(parts) >= 1
    assert all(tg_md.utf16_len(p) <= 4096 for p in parts)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
