"""Matrix message rendering for a markdown subset (#1780).

Two pure helpers with no Matrix client dependency:

* :func:`render_matrix_message` turns assistant text into ``(body,
  formatted_body)``. ``body`` is the untouched plain text (the fallback every
  client shows); ``formatted_body`` is a conservative HTML rendering of the
  markup the agents actually emit, or ``None`` when the text carries no
  markup at all so the transport can send a bare ``m.text``.
* :func:`chunk_text` splits long text on paragraph/line boundaries without
  ever cutting through a fenced code block (the fence is closed at the end of
  one chunk and reopened at the start of the next). Its limit is measured in
  **UTF-8 bytes**, because what bounds a Matrix event is a byte size and not a
  character count (#1828).

Safety model: every character of user/agent text is HTML-escaped *before* any
tag is inserted, tags are only ever emitted from fixed literals in this
module, and link targets must match ``http(s)://`` after escaping, so
``<script>`` or ``javascript:`` can only ever reach a client as literal text.
"""

from __future__ import annotations

from collections import deque
import html
import re

_FENCE_OPEN_RE = re.compile(r"^ {0,3}```[ \t]*([A-Za-z0-9_+#.-]{0,32})[ \t]*$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}```[ \t]*$")
_HEADING_RE = re.compile(r"^(#{1,3})[ \t]+(.*\S)[ \t]*$")
_UL_RE = re.compile(r"^ {0,3}[-*][ \t]+(?P<text>.*)$")
_OL_RE = re.compile(r"^ {0,3}(?P<num>\d{1,9})[.)][ \t]+(?P<text>.*)$")
_QUOTE_RE = re.compile(r"^ {0,3}>[ \t]?(.*)$")

_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
# Applied to *escaped* text: the URL charset is what survives html.escape for
# a well-formed http(s) URL (``&`` -> ``&amp;`` etc.). Parentheses end the link.
_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[\w\-.~:/?#\[\]@!$&+,;=%']+)\)")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)")
_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")

_MIN_CHUNK_LIMIT = 64
_FENCE_CLOSE_COST = 4  # "\n```" — ASCII, so its byte cost equals its length


def _w(text: str) -> int:
    """UTF-8 byte width of ``text``.

    ``chunk_text`` budgets in bytes, not characters: a Matrix event is bounded
    by the homeserver's PDU byte limit, and one Korean/CJK character costs
    three bytes. Sizing in characters let a 12,000-character Korean answer
    build a ~36 KB ``body`` (plus a comparable ``formatted_body`` in the same
    event), which overruns the 65,536-byte limit and used to surface as an
    unretryable 413 (#1828).
    """

    return len(text.encode("utf-8"))


def _head_within(text: str, budget: int) -> str:
    """Longest prefix of ``text`` that fits ``budget`` bytes, never splitting a character.

    Decoding with ``errors="ignore"`` drops a trailing partial sequence, so the
    prefix is always whole characters. Callers resume from ``text[len(head):]``,
    which re-includes the character that was cut in half — nothing is lost.
    At least one character is always returned so a hard cut cannot stall.
    """

    if budget <= 0 or not text:
        return text[:1]
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    head = raw[:budget].decode("utf-8", errors="ignore")
    return head or text[:1]


class _Inline:
    """Inline renderer that also records whether any markup was applied."""

    def __init__(self) -> None:
        self.markup = False

    def render(self, raw: str) -> str:
        stash: list[str] = []

        def stash_html(rendered: str) -> str:
            stash.append(rendered)
            self.markup = True
            return _PLACEHOLDER.format(len(stash) - 1)

        # 1. Code spans are opaque: nothing inside them is markup.
        text = _CODE_SPAN_RE.sub(
            lambda m: stash_html(f"<code>{html.escape(m.group(1), quote=True)}</code>"), raw
        )
        # 2. Escape everything that is left before inserting any tag.
        text = html.escape(text, quote=True)
        # 3. Links are stashed too so emphasis can never fire inside an href.
        text = _LINK_RE.sub(
            lambda m: stash_html(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text
        )
        text, n_bold = _BOLD_RE.subn(r"<strong>\1</strong>", text)
        text, n_star = _ITALIC_STAR_RE.subn(r"<em>\1</em>", text)
        text, n_under = _ITALIC_UNDERSCORE_RE.subn(r"<em>\1</em>", text)
        if n_bold or n_star or n_under:
            self.markup = True
        return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)


def _render_blocks(lines: list[str], inline: _Inline) -> list[str]:  # noqa: C901
    """Block-level pass; each branch is one markdown construct."""

    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        opener = _FENCE_OPEN_RE.match(line)
        if opener:
            inline.markup = True
            lang = opener.group(1)
            i += 1
            code: list[str] = []
            while i < n and not _FENCE_CLOSE_RE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence (or EOF)
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code), quote=True)}</code></pre>")
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            inline.markup = True
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline.render(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _QUOTE_RE.match(line):
            inline.markup = True
            quoted: list[str] = []
            while i < n and (m := _QUOTE_RE.match(lines[i])):
                quoted.append(inline.render(m.group(1)))
                i += 1
            out.append(f"<blockquote>{'<br>'.join(quoted)}</blockquote>")
            continue
        for tag, pattern in (("ul", _UL_RE), ("ol", _OL_RE)):
            if pattern.match(line):
                inline.markup = True
                rendered, i = _render_list(lines, i, tag, pattern, inline)
                out.append(rendered)
                break
        else:
            para: list[str] = []
            while i < n and lines[i].strip() and not _is_block_start(lines[i]):
                para.append(inline.render(lines[i]))
                i += 1
            out.append(f"<p>{'<br>'.join(para)}</p>")
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_continuation(line: str, item_indent: int) -> bool:
    """A non-blank line indented 2+ spaces past its item that starts no other block."""

    return bool(
        line.strip()
        and _indent(line) >= item_indent + 2
        and not _FENCE_OPEN_RE.match(line)
        and not _QUOTE_RE.match(line)
    )


def _render_list(
    lines: list[str], i: int, tag: str, pattern: re.Pattern[str], inline: _Inline
) -> tuple[str, int]:
    """Render the list starting at ``lines[i]``; return ``(html, next_index)``.

    Lines indented 2+ spaces past an item belong to it: bullets become a nested
    ``<ul>``, anything else is appended with ``<br>``. A single blank line
    between two items of the same list does not end the list (#1937).
    """

    n = len(lines)
    items: list[str] = []
    start = 1
    while i < n and (m := pattern.match(lines[i])):
        if not items and tag == "ol":
            start = int(m.group("num"))
        item_indent = _indent(lines[i])
        parts = [inline.render(m.group("text"))]
        nested: list[str] = []
        i += 1
        while i < n and _is_continuation(lines[i], item_indent):
            sub = _UL_RE.match(lines[i].lstrip(" "))
            if sub:
                nested.append(f"<li>{inline.render(sub.group('text'))}</li>")
            else:
                if nested:
                    parts.append(f"<ul>{''.join(nested)}</ul>")
                    nested = []
                else:
                    parts.append("<br>")
                parts.append(inline.render(lines[i].strip()))
            i += 1
        if nested:
            parts.append(f"<ul>{''.join(nested)}</ul>")
        items.append(f"<li>{''.join(parts)}</li>")
        if (
            i + 1 < n
            and not lines[i].strip()
            and pattern.match(lines[i + 1])
            and _indent(lines[i + 1]) < item_indent + 2
        ):
            i += 1
    attr = f' start="{start}"' if tag == "ol" and start != 1 else ""
    return f"<{tag}{attr}>{''.join(items)}</{tag}>", i


def _is_block_start(line: str) -> bool:
    return bool(
        _FENCE_OPEN_RE.match(line)
        or _HEADING_RE.match(line)
        or _QUOTE_RE.match(line)
        or _UL_RE.match(line)
        or _OL_RE.match(line)
    )


def render_matrix_message(text: str) -> tuple[str, str | None]:
    """Return ``(body, formatted_body)`` for one outbound Matrix text event.

    ``formatted_body`` is ``None`` when the text has no markup: plain text
    (including multi-line plain text) needs no ``org.matrix.custom.html``.
    """

    body = str(text or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    if not body.strip():
        return body, None
    inline = _Inline()
    blocks = _render_blocks(body.split("\n"), inline)
    if not inline.markup:
        return body, None
    return body, "".join(blocks)


def _fence_state(line: str, in_fence: bool) -> tuple[bool, bool, bool]:
    """Return ``(opens, closes, in_fence_after)`` for one line."""

    if in_fence:
        closes = bool(_FENCE_CLOSE_RE.match(line))
        return False, closes, not closes
    opens = bool(_FENCE_OPEN_RE.match(line))
    return opens, False, opens


def chunk_text(text: str, limit: int = 12_000) -> list[str]:  # noqa: C901
    """Split ``text`` into pieces of at most ``limit`` **UTF-8 bytes**.

    Preference order for a cut: the last blank line outside a fence, then a
    line boundary, then (only for a single oversized line) a hard cut. A
    fenced block that spans a cut is closed with ````` ``` ````` at the end of
    the chunk and reopened with its original opener line at the start of the
    next one, so every chunk renders as balanced markdown on its own.

    The limit counts bytes rather than characters (#1828). For ASCII the two
    agree, so existing behaviour is unchanged; for Korean/CJK a chunk is now
    bounded by what the homeserver actually measures. A hard cut never splits a
    character — see :func:`_head_within`.
    """

    if limit < _MIN_CHUNK_LIMIT:
        raise ValueError(f"chunk limit must be at least {_MIN_CHUNK_LIMIT}")
    text = str(text or "")
    if not text:
        return []
    if _w(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_fence = False
    opener = "```"
    last_break: int | None = None
    queue: deque[str] = deque(text.split("\n"))

    def emit(lines: list[str]) -> None:
        joined = "\n".join(lines)
        if joined.strip():
            chunks.append(joined)

    def reset() -> None:
        nonlocal current, current_len, in_fence, opener, last_break
        current, current_len, in_fence, opener, last_break = [], 0, False, "```", None

    while queue:
        line = queue.popleft()
        opens, closes, fence_after = _fence_state(line, in_fence)
        reserve = _FENCE_CLOSE_COST if fence_after else 0
        sep = 1 if current else 0
        new_len = current_len + sep + _w(line)
        if new_len + reserve <= limit:
            if opens:
                opener = line if _w(line) <= 40 else "```"
            current.append(line)
            current_len = new_len
            in_fence = fence_after
            if not in_fence and not line.strip():
                last_break = len(current) - 1
            continue

        # Overflow: choose the cut.
        if last_break is not None and last_break > 0:
            head, tail = current[:last_break], current[last_break + 1:]
            emit(head)
            reset()
            queue.extendleft(reversed(tail + [line]))
            continue
        if in_fence and len(current) > 1:
            emit(current + ["```"])
            reopen = opener
            reset()
            queue.appendleft(line)
            queue.appendleft(reopen)
            continue
        if current and not (in_fence and len(current) == 1):
            emit(current)
            reset()
            queue.appendleft(line)
            continue
        # A single line that cannot fit even in a fresh chunk: hard cut.
        cap = limit - current_len - sep - reserve
        if in_fence and current == [opener]:
            cap = limit - _w(opener) - 1 - _FENCE_CLOSE_COST
        cap = max(cap, 1)
        piece = _head_within(line, cap)
        current.append(piece)
        current_len += sep + _w(piece)
        queue.appendleft(line[len(piece):])
        if in_fence:
            emit(current + ["```"])
            reopen = opener
            reset()
            queue.appendleft(reopen)
        else:
            emit(current)
            reset()
    if in_fence and current:
        current.append("```")
    emit(current)
    return chunks
