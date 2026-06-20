"""Telegram-friendly Markdown formatting helpers.

Ported from the Hermes gateway Telegram adapter
(``gateway/platforms/telegram.py``) for the ccc-node bridge. Telegram has no
table syntax, so GFM pipe tables render as unreadable runs of ``|`` on mobile
clients. ``wrap_markdown_tables`` rewrites them into bold-headed bullet groups.

Pure string transforms — no Telegram/PTB dependency — so this module is safe to
unit-test in isolation and to call before message splitting.
"""

import re

# Matches a GFM table delimiter row: optional outer pipes, cells containing
# only dashes (with optional leading/trailing colons for alignment) separated
# by '|'.  Requires at least one internal '|' so lone '---' horizontal rules
# are NOT matched.
_TABLE_SEPARATOR_RE = re.compile(
    r'^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$'
)


def _is_table_row(line: str) -> bool:
    """Return True if *line* could plausibly be a table data row."""
    stripped = line.strip()
    return bool(stripped) and '|' in stripped


def _split_markdown_table_row(line: str) -> list:
    """Split a simple GFM table row into stripped cell values."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_block_for_telegram(table_block: list) -> str:
    """Render a detected GFM table as Telegram-friendly row groups."""
    if len(table_block) < 3:
        return "\n".join(table_block)

    headers = _split_markdown_table_row(table_block[0])
    if len(headers) < 2:
        return "\n".join(table_block)

    # Detect row-label column: present when data rows have one more cell
    # than the header row (the row-label column carries no header).
    first_data_row = _split_markdown_table_row(table_block[2]) if len(table_block) > 2 else []
    has_row_label_col = len(first_data_row) == len(headers) + 1

    rendered_groups = []
    for index, row in enumerate(table_block[2:], start=1):
        cells = _split_markdown_table_row(row)
        if has_row_label_col:
            # First cell is the row-label (heading); remaining cells align with headers.
            heading = cells[0] if cells and cells[0] else f"Row {index}"
            data_cells = cells[1:]
        else:
            # No row-label column: use first non-empty cell as heading.
            heading = next((cell for cell in cells if cell), f"Row {index}")
            data_cells = cells

        # Pad or trim data_cells to match headers length.
        if len(data_cells) < len(headers):
            data_cells.extend([""] * (len(headers) - len(data_cells)))
        elif len(data_cells) > len(headers):
            data_cells = data_cells[: len(headers)]

        # Build the bulleted lines for this row.  Skip any bullet whose value
        # duplicates the heading text -- when has_row_label_col is False the
        # heading IS the first data cell, and emitting it twice (once as the
        # bold heading, once as the first bullet) is visual noise.
        bullets = []
        for header, value in zip(headers, data_cells):
            if not has_row_label_col and value == heading:
                continue
            bullets.append(f"• {header}: {value}")

        # Within a row-group: single newline between heading and its bullets,
        # and between successive bullets.  This keeps the row visually tight
        # on Telegram instead of stretching each bullet into its own paragraph.
        group_lines = [f"**{heading}**", *bullets]
        rendered_groups.append("\n".join(group_lines))

    # Between row-groups: blank line so each group reads as a distinct block.
    return "\n\n".join(rendered_groups)


def wrap_markdown_tables(text: str) -> str:
    """Rewrite GFM-style pipe tables into Telegram-friendly bullet groups.

    Detected by a row containing '|' immediately followed by a delimiter
    row matching :data:`_TABLE_SEPARATOR_RE`.  Subsequent pipe-containing
    non-blank lines are consumed as the table body and rewritten as
    per-row bullet groups. Tables inside existing fenced code blocks are left
    alone.
    """
    if not text or '|' not in text or '-' not in text:
        return text

    lines = text.split('\n')
    out = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        # Track existing fenced code blocks — never touch content inside.
        if stripped.startswith('```'):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        # Look for a header row (contains '|') immediately followed by a
        # delimiter row.
        if (
            '|' in line
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            table_block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j]):
                table_block.append(lines[j])
                j += 1
            out.append(_render_table_block_for_telegram(table_block))
            i = j
            continue

        out.append(line)
        i += 1

    return '\n'.join(out)
