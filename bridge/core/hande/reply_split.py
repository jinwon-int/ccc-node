"""Split a long final answer into page-numbered bubbles (#2856).

The native bot bounds every message it posts (``reply_max_bytes``, 8 KiB by
default) and cuts the rest with ``…(길어서 줄임)``. A final answer above that is
therefore split here, before it reaches the bot: each part fits one bot message
and starts with a ``(k/N)`` page marker.

Pure text handling, no I/O. Rules, in order:

* an answer that fits one message is returned unchanged, without a marker;
* cuts prefer a heading, then a blank line, then a list item, then any line;
* a cut inside a code fence closes the fence and reopens it in the next part;
* a cut inside a table repeats the table header in the next part;
* the last ``[답변 필요]`` block (the choice card, one per message) stays whole
  in the last part whenever it fits one.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

# ``(999/999)`` plus the blank line after it.
MARKER_RESERVE_BYTES = 16
# A cut inside a fence appends ``\n``` `` to the part.
_FENCE = "```"
_FENCE_CLOSE_BYTES = len(_FENCE) + 1
_FENCE_OPENER_MAX_BYTES = 64
_ANSWER_HEADERS = ("[답변 필요]", "[answer needed]")
_HEADING = re.compile(r"^#{1,6}\s")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")

# Cut priorities (higher is better). ``_NEVER`` is used only when nothing else exists.
_PRIO_HEADING = 4
_PRIO_PARAGRAPH = 3
_PRIO_LIST = 2
_PRIO_LINE = 1
_PRIO_INSIDE = 0
_NEVER = -1


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass
class _Atom:
    """One line, or one piece of a line too long for a part."""

    text: str
    # Continues the previous atom on the same line (no newline in between).
    cont: bool = False
    # The fence opener in force before / after this atom (None outside a fence).
    fence: str | None = None
    fence_after: str | None = None
    # Table header rows to repeat when a part starts with this atom.
    table: tuple[str, str] | None = None
    prio: int = _PRIO_LINE


def _hard_pieces(line: str, piece_max: int) -> list[str]:
    """Cut one over-long line at a space when one is near, else at a character."""

    if _size(line) <= piece_max:
        return [line]
    pieces: list[str] = []
    rest = line
    while _size(rest) > piece_max:
        head = rest.encode("utf-8")[:piece_max].decode("utf-8", errors="ignore")
        space = head.rfind(" ")
        if space >= len(head) * 3 // 4:
            head = head[: space + 1]
        if not head:  # piece_max below one character: never loop forever
            head = rest[0]
        pieces.append(head)
        rest = rest[len(head):]
    if rest:
        pieces.append(rest)
    return pieces


def _plain_prio(line: str, prev_blank: bool) -> int:
    if _HEADING.match(line):
        return _PRIO_HEADING
    if prev_blank:
        return _PRIO_PARAGRAPH
    if _LIST_ITEM.match(line):
        return _PRIO_LIST
    return _PRIO_LINE


class _Scan:
    """Fence and table state while the lines are read top to bottom."""

    def __init__(self, header_max: int) -> None:
        self.header_max = header_max
        self.fence: str | None = None
        self.table_rows = 0
        self.header: tuple[str, str] | None = None
        self.first_row = ""
        self.prev_blank = True

    def inside_fence(self, atom: _Atom) -> None:
        closes = atom.text.lstrip().startswith(_FENCE)
        # Before the closing line a cut would leave an empty reopened block.
        atom.prio = _NEVER if closes else _PRIO_INSIDE
        if closes:
            self.fence = None
        self.prev_blank = False
        self.table_rows = 0
        self.header = None

    def outside_fence(self, atom: _Atom) -> None:
        line = atom.text
        stripped = line.lstrip()
        if stripped.startswith("|"):
            self.table_rows += 1
        else:
            self.table_rows = 0
            self.header = None
        if self.table_rows == 1:
            self.first_row = line
        if self.table_rows == 2:
            # Never between the header row and its separator.
            atom.prio = _NEVER
            fits = _size(self.first_row) + _size(line) + 2 <= self.header_max
            if _TABLE_SEPARATOR.match(line) and fits:
                self.header = (self.first_row, line)
        elif self.table_rows > 2:
            atom.prio = _PRIO_INSIDE
            atom.table = self.header
        else:
            atom.prio = _plain_prio(line, self.prev_blank)
        if stripped.startswith(_FENCE):
            opener = line.strip()
            self.fence = opener if _size(opener) <= _FENCE_OPENER_MAX_BYTES else _FENCE
        self.prev_blank = not line.strip()


def _annotate(body: str, piece_max: int, header_max: int) -> list[_Atom]:
    atoms: list[_Atom] = []
    for line in body.split("\n"):
        for index, piece in enumerate(_hard_pieces(line, piece_max)):
            atoms.append(_Atom(piece, cont=index > 0))
    scan = _Scan(header_max)
    for atom in atoms:
        atom.fence = scan.fence
        if atom.cont:
            atom.prio = _PRIO_INSIDE
        elif scan.fence is not None:
            scan.inside_fence(atom)
        else:
            scan.outside_fence(atom)
        atom.fence_after = scan.fence
    return atoms


def _choose_cut(atoms: list[_Atom], start: int, end: int, filled: list[int], budget: int) -> int:
    """Where to cut in ``(start, end]``: the best boundary in the fuller half."""

    half = budget // 2
    for prio in (_PRIO_HEADING, _PRIO_PARAGRAPH, _PRIO_LIST, _PRIO_LINE):
        for cut in range(end, start, -1):
            if atoms[cut].prio == prio and filled[cut - start] >= half:
                return cut
    for floor in (_PRIO_LINE, _PRIO_INSIDE):
        for cut in range(end, start, -1):
            if atoms[cut].prio >= floor:
                return cut
    return end


def _join(atoms: list[_Atom]) -> str:
    out: list[str] = []
    for index, atom in enumerate(atoms):
        if index and not atom.cont:
            out.append("\n")
        out.append(atom.text)
    return "".join(out)


def _pack(body: str, budget: int) -> list[str]:
    """Greedy parts of at most ``budget`` bytes each."""

    atoms = _annotate(body, piece_max=max(1, budget // 2), header_max=budget // 4)
    total = len(atoms)
    parts: list[str] = []
    prefix: list[str] = []
    start = 0
    while start < total:
        while (
            start < total
            and atoms[start].fence is None
            and not atoms[start].cont
            and not atoms[start].text.strip()
        ):
            start += 1  # a part never starts with blank lines
        if start >= total:
            break
        size = sum(_size(line) + 1 for line in prefix)
        # filled[i]: bytes used before atom ``start + i``.
        filled = [size]
        end = start
        while end < total:
            atom = atoms[end]
            add = _size(atom.text) + 1
            reserve = _FENCE_CLOSE_BYTES if atom.fence_after is not None else 0
            if end > start and size + add + reserve > budget:
                break
            size += add
            end += 1
            filled.append(size)
        cut = total if end >= total else _choose_cut(atoms, start, end, filled, budget)
        text = _join(atoms[start:cut])
        in_fence = cut < total and atoms[cut].fence is not None
        if in_fence:
            text = f"{text}\n{_FENCE}"
        else:
            text = text.rstrip("\n")
        parts.append("\n".join([*prefix, text]))
        prefix = []
        if cut < total:
            if atoms[cut].fence is not None:
                prefix = [atoms[cut].fence or _FENCE]
            elif atoms[cut].table is not None:
                prefix = list(atoms[cut].table or ())
        start = cut
    return parts


def answer_block_start(text: str) -> int | None:
    """Offset of the last ``[답변 필요]`` block, the bot's own rule (``answer_block_span``).

    A header line outside a code fence, followed (blank lines aside) by a
    numbered option. ``None`` when the answer has no such block.
    """

    lines: list[tuple[int, str]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        lines.append((offset, raw.rstrip("\r\n")))
        offset += len(raw)
    found: int | None = None
    fenced = False
    for index, (start, line) in enumerate(lines):
        if line.lstrip().startswith(_FENCE):
            fenced = not fenced
            continue
        if fenced:
            continue
        clean = line.replace("**", "").replace("__", "").replace("`", "").lower()
        if not any(header in clean for header in _ANSWER_HEADERS):
            continue
        follow = index + 1
        while follow < len(lines) and not lines[follow][1].strip():
            follow += 1
        if follow < len(lines) and _NUMBERED.match(lines[follow][1]):
            found = start
    return found


def split_reply(text: str, *, single_bytes: int, part_bytes: int) -> list[str]:
    """Parts of ``text`` for one bot message each.

    ``single_bytes`` is what one unsplit answer may be; at or under it the
    answer comes back alone and unmarked. Otherwise every part — its ``(k/N)``
    marker included — is at most ``part_bytes``.
    """

    if _size(text) <= single_bytes:
        return [text]
    budget = part_bytes - MARKER_RESERVE_BYTES
    if budget < 64:
        return [text]
    body, tail = text, ""
    block = answer_block_start(text)
    if block is not None:
        candidate = text[block:].rstrip()
        # A block too long for one part cannot be kept whole; split it like prose.
        if candidate and _size(candidate) <= budget:
            body, tail = text[:block], candidate
    parts = _pack(body.strip("\n"), budget) if body.strip() else []
    if tail:
        if parts and _size(parts[-1]) + 2 + _size(tail) <= budget:
            parts[-1] = f"{parts[-1]}\n\n{tail}"
        else:
            parts.append(tail)
    if len(parts) < 2:
        return parts or [text]
    count = len(parts)
    return [f"({index}/{count})\n\n{part}" for index, part in enumerate(parts, 1)]
