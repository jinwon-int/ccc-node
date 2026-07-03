"""Readable text normalization for Telegram output (GitHub issue #34, slice 1).

Pure, conservative string transforms that improve mobile readability WITHOUT
changing message *content* or risking delivery. When enabled via
``CCC_TELEGRAM_READABLE_RENDERER`` the final assistant text is passed through
``to_readable`` BEFORE the Markdown -> MarkdownV2 conversion in
``core/streaming.py``.

Design constraints (issue #34 "Non-goals / safety boundary"):
- Fail-open: any error returns the input unchanged so formatting never costs a
  message (delivery reliability beats formatting).
- Idempotent: ``to_readable(to_readable(x)) == to_readable(x)`` so snapshots are
  stable and re-rendering is safe.
- Content-preserving: only whitespace/blank-line layout is adjusted; words,
  code, tables, and links are left intact. Fenced code blocks are never touched.

Slice 1 scope (this module):
- strip trailing whitespace per line (outside fenced code blocks)
- collapse runs of blank lines down to a single blank line (outside fences)
- ensure a blank line before a heading / bold-only section label that directly
  follows non-blank content, so sections are scannable on mobile
- trim leading/trailing blank lines

Out of scope (later slices of #34): entity-based output, table reflow policy,
inline-code wrapping of paths/SHAs, chunk part headers.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# A markdown ATX heading: up to 3 leading spaces, 1-6 '#', a space, then content.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
# A line that is *only* a bold span (an operational section label), e.g.
# "**확인됨**" or "**Next steps**:". Used to give report-style sections air.
_BOLD_ONLY_RE = re.compile(r"^\s*\*\*[^*\n]+\*\*:?\s*$")
# A fenced code block delimiter.
_FENCE_RE = re.compile(r"^\s*```")
# A list item line: optional indent, a bullet (-, *, +, •) or number (1. / 1)),
# a space, then content. Used by loose spacing to give each item its own line.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+\S")

# Filler for extra gap lines when spacing > 1. The downstream Markdown ->
# MarkdownV2 / entity converters (telegramify) parse the text into an AST and
# re-render it, which collapses every run of truly blank lines back to a single
# blank line (and removes blank lines between list items entirely) — silently
# undoing any widening done here. A line holding a single NO-BREAK SPACE is a
# real (invisible) paragraph to the converter, so it survives conversion intact
# and renders as an empty line in Telegram. The first line of every gap stays a
# genuine blank line so Markdown block structure is unchanged.
GAP_FILLER_LINE = "\u00a0"  # NO-BREAK SPACE


def _is_list_item(line: str) -> bool:
    return bool(_LIST_ITEM_RE.match(line))


def _is_section_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line) or _BOLD_ONLY_RE.match(line))


def _gap_filler_count(prev_line: str, next_line: str, spacing: int) -> int:
    """Filler lines a gap needs so the *rendered* gap is uniform.

    The telegramify Markdown converter eats the gap's real blank line whenever
    the gap follows a list item (lazy-continuation parsing) but keeps it
    everywhere else, so a fixed filler count renders unevenly (the exact
    inconsistency reported on GitHub issue #34 follow-ups). Compensate per
    boundary so every rendered gap between blocks is `spacing` blank lines,
    list items sit one step tighter at `spacing - 1`, and a heading/section
    label always has exactly one blank line under it.
    """
    if _is_section_heading(prev_line):
        return 0  # heading gap stays a single real blank -> renders as 1
    if _is_list_item(prev_line):
        if _is_list_item(next_line):
            return spacing - 1  # blank eaten -> renders as spacing - 1
        return spacing  # blank eaten -> renders as spacing
    return spacing - 1  # blank kept -> renders as spacing


def to_readable(text: str, loose: bool = False, spacing: int = 1) -> str:
    """Return a readability-normalized copy of *text*.

    When *loose* is True, also insert a blank line between adjacent list-item
    lines so each item gets its own visual line — prose lines stay attached and
    fenced code is left intact.

    *spacing* sets how many visually blank lines separate blocks in the
    RENDERED message (clamped to [1, 3]): every paragraph/section gap shows
    ``spacing`` blank lines, list items sit one step tighter at
    ``spacing - 1``, and a heading/section label always has exactly one blank
    line under it. Gaps are emitted as one real blank line plus a
    boundary-dependent number of invisible :data:`GAP_FILLER_LINE` lines
    (see :func:`_gap_filler_count`) to compensate for the downstream
    Markdown -> MarkdownV2 / entity conversion, which collapses runs of truly
    blank lines and eats the blank after list items — the correction that
    keeps the rendered spacing uniform across boundary types.

    Fail-open: on any unexpected error the original *text* is returned unchanged.
    """
    try:
        return _transform(text, loose=loose, spacing=spacing)
    except Exception:  # pragma: no cover - never let formatting break delivery
        logger.warning(
            "tg_readable.to_readable failed; returning input unchanged",
            exc_info=True,
        )
        return text


def render_for_delivery(
    text: str, *, enabled: bool, loose: bool, spacing: int = 1
) -> str:
    """Apply the readable renderer for outbound delivery, honoring config flags.

    Single source of truth shared by BOTH the streaming finalize path
    (``core/streaming.py``) and the non-streaming delivery path
    (``core/bot.py``) so the two never drift: whenever the readable renderer is
    enabled, every reply is normalized the same way regardless of which path
    sends it. When *enabled* is False the text is returned unchanged.

    Fail-open via :func:`to_readable`.
    """
    if not enabled:
        return text
    return to_readable(text, loose=loose, spacing=spacing)


def _transform(text: str, loose: bool = False, spacing: int = 1) -> str:
    if not text:
        return text

    # Defensive clamp so a stray config value can never explode message length
    # or break the single-blank invariant relied on by downstream conversion.
    try:
        spacing = max(1, min(int(spacing), 3))
    except (TypeError, ValueError):
        spacing = 1

    lines = text.split("\n")

    # Pass 1: strip trailing whitespace, but leave fenced code content untouched.
    in_fence = False
    pass1: list[str] = []
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            pass1.append(line.rstrip())
            continue
        pass1.append(line if in_fence else line.rstrip())

    # Pass 2: ensure a blank line before a heading/section label that directly
    # follows content (outside fences).
    in_fence = False
    pass2: list[str] = []
    for line in pass1:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            pass2.append(line)
            continue
        if (
            not in_fence
            and _is_section_heading(line)
            and pass2
            and pass2[-1].strip() != ""
        ):
            pass2.append("")
        pass2.append(line)

    # Pass 2b: ensure a blank line after a heading/section label so body text
    # never sits attached directly under its title. Pass 3 keeps this gap at
    # exactly one real blank line (uniform "one blank line under every
    # heading"), which both converter paths preserve.
    in_fence = False
    pass2b: list[str] = []
    for line in pass2:
        is_fence_delim = bool(_FENCE_RE.match(line))
        if (
            not in_fence
            and pass2b
            and _is_section_heading(pass2b[-1])
            and line.strip() != ""
        ):
            pass2b.append("")
        pass2b.append(line)
        if is_fence_delim:
            in_fence = not in_fence
    pass2 = pass2b

    # Pass 2.5 (opt-in): loose spacing — insert a single blank line between two
    # adjacent list-item lines so each item gets its own visual line. Telegram has
    # no line-height control, so blank lines are the only way to "space out" a
    # dense bullet/numbered list. Prose lines are left attached (only list items
    # are spaced), and fenced code is untouched. A list item followed by an
    # indented continuation line is NOT split (the continuation isn't a list item).
    if loose:
        in_fence = False
        loose_lines: list[str] = []
        for line in pass2:
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                loose_lines.append(line)
                continue
            if (
                not in_fence
                and _is_list_item(line)
                and loose_lines
                and _is_list_item(loose_lines[-1])
            ):
                loose_lines.append("")
            loose_lines.append(line)
        pass2 = loose_lines

    # Pass 3: normalize every run of blank lines (outside fences) to exactly one
    # real blank line plus a boundary-dependent number of GAP_FILLER_LINE lines
    # (see _gap_filler_count) so the gap the USER SEES after the downstream
    # Markdown -> MarkdownV2 / entity conversion is uniform: `spacing` blank
    # lines between blocks, `spacing - 1` between list items, exactly one under
    # a heading. The filler is needed because the converter collapses runs of
    # truly blank lines — see GAP_FILLER_LINE. Leading and trailing runs are
    # trimmed at the end, so interior gaps are the only ones widened —
    # soft-wrapped lines with no blank between them stay attached.
    in_fence = False
    pass3: list[str] = []
    i = 0
    n = len(pass2)
    while i < n:
        line = pass2[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            pass3.append(line)
            i += 1
            continue
        if not in_fence and line.strip() == "":
            # Consume the whole blank run (NBSP filler counts as blank, which
            # keeps the transform idempotent), then emit the canonical gap for
            # this boundary. Leading/trailing runs get no filler — they are
            # trimmed below anyway.
            j = i
            while (
                j < n
                and not _FENCE_RE.match(pass2[j])
                and pass2[j].strip() == ""
            ):
                j += 1
            pass3.append("")
            if pass3[:-1] and j < n:
                pass3.extend(
                    [GAP_FILLER_LINE]
                    * _gap_filler_count(pass3[-2], pass2[j], spacing)
                )
            i = j
        else:
            pass3.append(line)
            i += 1

    # Trim leading/trailing blank lines (including NBSP filler, which is
    # whitespace to str.strip) without disturbing interior content.
    start, end = 0, len(pass3)
    while start < end and pass3[start].strip() == "":
        start += 1
    while end > start and pass3[end - 1].strip() == "":
        end -= 1
    return "\n".join(pass3[start:end])


# Headroom (UTF-16 units) reserved so a part marker like "*12/12*\n" can never
# push an already limit-sized chunk past the Telegram message limit. The caller
# shrinks the split limit by this amount before splitting.
PART_HEADER_RESERVE = 16


def part_marker(index: int, total: int) -> str:
    """Return a compact, MarkdownV2-safe part marker, e.g. ``*2/3*``.

    Uses a bold span (`*...*`); digits and ``/`` are not MarkdownV2 special
    characters, so no escaping is required.
    """
    return f"*{index}/{total}*"


def apply_part_headers(parts):
    """Prefix each chunk with a compact ``k/N`` continuation marker.

    Returns a new list. A single chunk (or empty input) is returned unchanged —
    a part marker is only meaningful when a response spans multiple messages.
    Each chunk is assumed to already be MarkdownV2; the marker is MarkdownV2-safe
    and is separated from the body by a single newline.
    """
    parts = list(parts)
    total = len(parts)
    if total <= 1:
        return parts
    return [f"{part_marker(i, total)}\n{chunk}" for i, chunk in enumerate(parts, 1)]
