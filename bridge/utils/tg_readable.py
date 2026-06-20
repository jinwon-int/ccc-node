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


def _is_section_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line) or _BOLD_ONLY_RE.match(line))


def to_readable(text: str) -> str:
    """Return a readability-normalized copy of *text*.

    Fail-open: on any unexpected error the original *text* is returned unchanged.
    """
    try:
        return _transform(text)
    except Exception:  # pragma: no cover - never let formatting break delivery
        logger.warning(
            "tg_readable.to_readable failed; returning input unchanged",
            exc_info=True,
        )
        return text


def _transform(text: str) -> str:
    if not text:
        return text

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

    # Pass 3: collapse runs of blank lines to a single blank line (outside fences).
    in_fence = False
    blank_run = 0
    pass3: list[str] = []
    for line in pass2:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            blank_run = 0
            pass3.append(line)
            continue
        if not in_fence and line.strip() == "":
            blank_run += 1
            if blank_run >= 2:
                continue  # keep at most one consecutive blank line
            pass3.append("")
        else:
            blank_run = 0
            pass3.append(line)

    # Trim leading/trailing blank lines without disturbing interior content.
    return "\n".join(pass3).strip("\n")


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
