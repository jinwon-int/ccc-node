"""Bounded JSON syntax only; never pre-stop schema validation or authority.

No callers, filesystem access, peer authentication or lifecycle effects belong
here. The caller must bound its read before allocating input and independently
validate the versioned schema and authenticated state before using any values.
"""

from __future__ import annotations

import json
from typing import Any

MAX_BYTES = 16 * 1024
MAX_DEPTH = 8
MIN_INTEGER = -(2**63)
MAX_INTEGER = 2**63 - 1


class PrestopJSONError(ValueError):
    """Invalid input; messages deliberately omit untrusted record content."""


def _check_depth(text: str) -> None:
    # Scan before the recursive stdlib decoder. Grammar validation remains its
    # responsibility; braces and escaped quotes inside strings are not nesting.
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise PrestopJSONError("JSON nesting limit exceeded")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise PrestopJSONError("Invalid JSON structure")


def _integer(text: str) -> int:
    # Bound work independently of the interpreter's optional integer limit.
    if len(text.lstrip("-")) > 19:
        raise PrestopJSONError("JSON integer out of range")
    value = int(text)
    if not MIN_INTEGER <= value <= MAX_INTEGER:
        raise PrestopJSONError("JSON integer out of range")
    return value


def _reject_number(_text: str) -> Any:
    raise PrestopJSONError("Non-integer JSON number forbidden")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrestopJSONError("Duplicate JSON key")
        result[key] = value
    return result


def decode_object(data: bytes) -> dict[str, Any]:
    """Decode at most 16 KiB, with at most eight object/array nesting levels.

    Only exact bytes input is accepted. Objects, arrays, scalar strings, signed
    64-bit integers, booleans and null are syntactically valid; the root must be
    an object. Field names, versions, boolean-vs-integer types and all authority
    checks are intentionally left to a future strict schema layer.
    """
    if type(data) is not bytes:
        raise PrestopJSONError("Expected bytes")
    if len(data) > MAX_BYTES:
        raise PrestopJSONError("JSON byte limit exceeded")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PrestopJSONError("Invalid UTF-8") from None
    if text.startswith("\ufeff"):
        raise PrestopJSONError("UTF-8 BOM forbidden")
    _check_depth(text)
    try:
        result = json.loads(
            text,
            object_pairs_hook=_object,
            parse_int=_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except json.JSONDecodeError:
        raise PrestopJSONError("Invalid JSON syntax") from None
    if not isinstance(result, dict):
        raise PrestopJSONError("JSON root must be an object")
    pending: list[Any] = [result]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise PrestopJSONError("Unpaired Unicode surrogate forbidden")
    return result
