#!/usr/bin/env python3
"""YAML-safe single-line SKILL.md frontmatter scalars (#2032).

Every ccc-node skill reader parses SKILL.md frontmatter line by line (one
``key: value`` per line; nodes may lack PyYAML), while the runtimes and
fleet-skills ``validate.py`` (fleet-skills#328) parse the same block as YAML.
Both readers must see the same ``description``. An unquoted value containing
``": "`` is invalid YAML, and one containing ``" #"`` loses its tail to a YAML
comment, so writers render values through :func:`render_scalar` (plain when
provably safe, otherwise one double-quoted line) and line readers decode them
through :func:`unquote_scalar`.

Stdlib only. setup.sh installs this file verbatim as
``~/.claude/hooks/ccc_skill_frontmatter.py``; ``scripts/ccc_skill_frontmatter.py``
re-exports it for repository runs (the ``ccc_secure_fs`` convention).

CLI (used by the shell autosave writers)::

    skill_frontmatter.py normalize <in> [<out>]   rewrite description line(s)
    skill_frontmatter.py render                   stdin value -> YAML scalar
    skill_frontmatter.py unquote                  stdin raw   -> value

``normalize`` exits 0 on success and 3 when the rendered value cannot be
proven YAML-safe (the caller keeps the draft blocked).
"""

from __future__ import annotations

import re
import sys
from typing import Any

try:  # Optional: only used to double-check the stdlib renderer.
    import yaml as _yaml
except Exception:  # pragma: no cover - PyYAML absent (many nodes)
    _yaml = None

# First characters that start a YAML token other than a plain scalar
# (YAML 1.1/1.2 indicators, plus the reserved backtick and "@").
_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
# Values a YAML 1.1 resolver (PyYAML) turns into something other than a str.
_RESOLVED_RE = re.compile(
    r"""^(?:
        y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE
        |on|On|ON|off|Off|OFF
        |~|null|Null|NULL
        |=|<<
        |[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)
    )$""",
    re.X,
)
# Numbers, dates, and sexagesimal forms all start with a digit, or a sign/dot
# followed by one. Quoting all of them is deliberately conservative.
_NUMERIC_START_RE = re.compile(r"^[-+.]?[0-9]")
_LINE_BREAK_RE = re.compile("[ \t]*(?:\r\n|[\r\n\x85  ])[ \t]*")
# Characters PyYAML accepts in a stream (yaml.reader.Reader.NON_PRINTABLE
# complement); anything else must be escaped inside double quotes.
_PRINTABLE_RE = re.compile("[\x20-\x7e\x85\xa0-퟿-﻾＀-�\U00010000-\U0010ffff]")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][-+0-9]*[ \t]*(?:#.*)?$")

_SIMPLE_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\0": "\\0",
    "\a": "\\a",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\v": "\\v",
    "\f": "\\f",
    "\r": "\\r",
    "\x1b": "\\e",
    "\x85": "\\N",
    " ": "\\L",
    " ": "\\P",
}
_DECODE_SIMPLE = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": " ",
    "P": " ",
}
_DECODE_HEX = {"x": 2, "u": 4, "U": 8}
_TRAILER_RE = re.compile(r"^(?:[ \t]+#.*)?[ \t]*$")


def collapse(value: str) -> str:
    """Fold every line break (and the blanks around it) into one space."""
    return _LINE_BREAK_RE.sub(" ", value)


def needs_quoting(value: str) -> bool:
    """True unless ``value`` is certainly a plain YAML scalar equal to itself."""
    if not value or value != value.strip(" \t"):
        return True
    if value[0] in _INDICATORS or value.endswith(":"):
        return True
    if ": " in value or " #" in value or "\t" in value:
        return True
    if _RESOLVED_RE.match(value) or _NUMERIC_START_RE.match(value):
        return True
    return any(not _PRINTABLE_RE.match(char) for char in value)


def double_quote(value: str) -> str:
    """One-line YAML double-quoted scalar for ``value``."""
    out = ['"']
    for char in value:
        escaped = _SIMPLE_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif _PRINTABLE_RE.match(char):
            out.append(char)
        elif ord(char) <= 0xFF:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(f"\\u{ord(char):04x}")
    out.append('"')
    return "".join(out)


def _yaml_value(raw: str) -> Any:
    """What PyYAML reads for ``key: <raw>``; raises on invalid YAML."""
    assert _yaml is not None
    data = _yaml.safe_load(f"k: {raw}\n")
    if not isinstance(data, dict) or set(data) != {"k"}:
        raise ValueError("not a single mapping entry")
    return data["k"]


def _yaml_agrees(raw: str, value: str) -> bool:
    if _yaml is None:
        return True
    try:
        return _yaml_value(raw) == value
    except Exception:
        return False


def render_scalar(value: str) -> str:
    """Single-line YAML scalar that every reader decodes back to ``value``.

    Line breaks are collapsed to spaces first (the frontmatter layout is one
    line per key). Plain output only when :func:`needs_quoting` says it is
    safe; where PyYAML is importable the result is additionally round-tripped
    through ``yaml.safe_load`` and a plain candidate falls back to quoting.
    """
    value = collapse(value)
    if not needs_quoting(value):
        if _yaml_agrees(value, value):
            return value
    quoted = double_quote(value)
    if not _yaml_agrees(quoted, value):  # pragma: no cover - renderer bug guard
        raise ValueError("skill_frontmatter_render_unsafe")
    return quoted


def _decode_double(raw: str) -> tuple[str, int] | None:
    """Decode a double-quoted scalar starting at raw[0]; (value, end) or None."""
    out: list[str] = []
    index = 1
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == '"':
            return "".join(out), index + 1
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= length:
            return None
        code = raw[index + 1]
        if code in _DECODE_SIMPLE:
            out.append(_DECODE_SIMPLE[code])
            index += 2
            continue
        width = _DECODE_HEX.get(code)
        digits = raw[index + 2 : index + 2 + width] if width else ""
        if not width or len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
            return None
        try:
            out.append(chr(int(digits, 16)))
        except (ValueError, OverflowError):
            return None
        index += 2 + width
    return None


def _decode_single(raw: str) -> tuple[str, int] | None:
    out: list[str] = []
    index = 1
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == "'":
            if raw[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            return "".join(out), index + 1
        out.append(char)
        index += 1
    return None


def parse_scalar(raw: str) -> tuple[str, bool]:
    """Decode the text after ``key:`` on one frontmatter line.

    Returns ``(value, well_formed)``. A complete single- or double-quoted
    scalar (optionally followed by `` # comment``) is decoded with YAML
    escapes. Anything else is returned stripped and verbatim — plain values
    keep a `` #...`` tail on purpose, because that is the text the author
    wrote (and what the line readers always used); :func:`is_yaml_safe`
    flags such lines so writers re-quote them. ``well_formed`` is False only
    for a value that opens a quote but does not close it cleanly.
    """
    raw = raw.strip()
    if not raw or raw[0] not in "\"'":
        return raw, True
    decoded = _decode_double(raw) if raw[0] == '"' else _decode_single(raw)
    if decoded is None or not _TRAILER_RE.match(raw[decoded[1] :]):
        return raw, False
    return decoded[0], True


def unquote_scalar(raw: str) -> str:
    """The value a line reader should use for ``key: <raw>``."""
    return parse_scalar(raw)[0]


def is_block_scalar(raw: str) -> bool:
    return bool(_BLOCK_SCALAR_RE.match(raw.strip()))


def is_yaml_safe(raw: str) -> bool:
    """True when ``key: <raw>`` means the same thing to YAML and line readers."""
    raw = raw.strip()
    value, well_formed = parse_scalar(raw)
    if not raw or not well_formed or is_block_scalar(raw):
        return False
    if raw[0] not in "\"'":
        if _yaml is None:
            return not needs_quoting(raw)
    return _yaml_agrees(raw, value) if _yaml is not None else True


def render_line(key: str, value: str) -> str:
    return f"{key}: {render_scalar(value)}"


def _frontmatter_bounds(lines: list[str]) -> int | None:
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            return index
    return None


def normalize_skill_md(text: str, keys: tuple[str, ...] = ("description",)) -> str:
    """Re-render the given single-line frontmatter keys YAML-safely.

    Idempotent. Values already written safely (plain or quoted) come out
    unchanged; block scalars and key-only lines are left alone for the
    structural gates to judge. Text without a frontmatter block is returned
    as-is.
    """
    lines = text.splitlines(keepends=True)
    end = _frontmatter_bounds(lines)
    if end is None:
        return text
    for index in range(1, end):
        line = lines[index]
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        key, sep, rest = body.partition(":")
        if not sep or key not in keys:
            continue
        raw = rest.strip()
        if not raw or is_block_scalar(raw):
            continue
        rendered = render_line(key, unquote_scalar(raw))
        if rendered != body:
            lines[index] = rendered + ending
    return "".join(lines)


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "normalize" and len(argv) in (3, 4):
        try:
            with open(argv[2], encoding="utf-8", newline="") as handle:
                text = handle.read()
            result = normalize_skill_md(text)
        except (OSError, UnicodeDecodeError):
            return 2
        except ValueError:
            return 3
        target = argv[3] if len(argv) == 4 else argv[2]
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(result)
        except OSError:
            return 2
        return 0
    if len(argv) == 2 and argv[1] in ("render", "unquote"):
        data = sys.stdin.read()
        if argv[1] == "render":
            try:
                sys.stdout.write(render_scalar(data.rstrip("\n")))
            except ValueError:
                return 3
        else:
            sys.stdout.write(unquote_scalar(data.rstrip("\n")))
        return 0
    sys.stderr.write(
        "usage: skill_frontmatter.py normalize <in> [<out>] | render | unquote\n"
    )
    return 64


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
