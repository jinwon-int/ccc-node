"""Node-local span ledger with OTel GenAI attribute names (#2684).

Writes one body-free span per line to ``<state>/spans/YYYY-MM-DD.jsonl`` (UTC
day of the write). Attribute names follow the OpenTelemetry GenAI semantic
conventions (``gen_ai.*``) plus ``ccc.*`` extensions, so the files can later be
exported to any collector without a rename; no collector, exporter or trace
propagation is involved here.

Write rules match :mod:`usage_cost_ledger`: append-only JSONL, one
self-contained line per span, corrupt/torn lines skipped on read, and every
public method swallows exceptions — the ledger must never break the
conversation path. On top of that:

* **Body-free by allowlist.** Only the keys in :data:`ALLOWED_KEYS` survive,
  each with a fixed scalar type. String values must look like an identifier
  (no whitespace, bounded charset), are capped at :data:`MAX_STRING_CHARS`, and
  pass through the shared credential redaction as defense in depth. Prompt,
  tool-input/output and answer text therefore cannot be stored even when a
  caller passes it by mistake.
* **Concurrent-writer safe.** A Telegram and a Matrix frontend on one project
  root share the directory, so each span is a single ``os.write`` of one line
  on an ``O_APPEND`` descriptor (no buffered partial writes).
* **Owner-only, no symlinks.** Directory ``0700``, files ``0600``, the day file
  is opened ``O_NOFOLLOW`` and a symlinked spans directory is refused.
* **Bounded retention.** :meth:`SpanLedger.prune` deletes only regular files
  named exactly ``YYYY-MM-DD.jsonl`` (a real date) older than the retention
  window; it runs at most once per UTC day per process from :meth:`record`.

There is deliberately no fsync: spans are telemetry, not accounting (the cost
ledger keeps the money-adjacent fsync), and a span write sits on the turn's
event-loop path. A crash can lose at most the last unflushed spans.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from telegram_bot.utils.redaction import REDACTION_MARKER, redact_credentials

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 14
MAX_RETENTION_DAYS = 366
MAX_STRING_CHARS = 128
#: Strings longer than this are cut before redaction so the regex pass stays
#: bounded on hostile input; the final value is capped at MAX_STRING_CHARS.
_PRE_REDACTION_CHARS = 1024
#: Hard upper bound for one serialized span (one write, well under PIPE_BUF).
MAX_LINE_BYTES = 4096
_MAX_COUNT = 10**12
_MAX_DURATION_MS = 10**10  # ~115 days

SPAN_KINDS = frozenset({"turn", "tool", "model", "a2a"})
SPAN_STATUSES = frozenset({"ok", "error", "timeout"})

#: ``gen_ai.operation.name`` for a whole agent turn (OTel GenAI well-known value).
OPERATION_INVOKE_AGENT = "invoke_agent"

_STR, _INT, _FLOAT = "str", "int", "float"

#: The complete set of keys a span may carry, with each key's scalar type.
ALLOWED_TYPES: Mapping[str, str] = {
    "kind": _STR,
    "status": _STR,
    "start_ts": _FLOAT,
    "duration_ms": _INT,
    "error.type": _STR,
    "gen_ai.operation.name": _STR,
    "gen_ai.request.model": _STR,
    "gen_ai.usage.input_tokens": _INT,
    "gen_ai.usage.output_tokens": _INT,
    "gen_ai.conversation.id": _STR,
    # Tool spans (core/tool_spans.py): the tool name only, never input/output.
    "gen_ai.tool.name": _STR,
    "ccc.tool.scope": _STR,
    "ccc.node": _STR,
    "ccc.channel": _STR,
    "ccc.turn_id": _STR,
    "ccc.skill": _STR,
    "ccc.outcome": _STR,
    # Extensions that let the rollup cross-check the cost ledger: OTel input
    # tokens include cached input, the ledger's ``in`` does not.
    "ccc.cost_usd": _FLOAT,
    "ccc.usage.cache_read_tokens": _INT,
    "ccc.usage.cache_creation_tokens": _INT,
}
ALLOWED_KEYS = frozenset(ALLOWED_TYPES)

#: Identifier-shaped values only: model ids (``glm-5.2[1m]``,
#: ``us.anthropic.claude-…``), node/channel names, uuids, subtype codes.
#: Whitespace is excluded, so free text cannot pass.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/@+\-\[\]]+$")
SPAN_FILE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def conversation_ref(value: object) -> Optional[str]:
    """Opaque, stable conversation id: a short salted hash, never the raw id."""

    if value is None:
        return None
    try:
        text = str(value)
    except Exception:
        return None
    if not text:
        return None
    return sha256(("ccc-span:" + text).encode("utf-8")).hexdigest()[:16]


def _clean_string(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    text = value[:_PRE_REDACTION_CHARS]
    if redact_credentials(text) != text:
        return REDACTION_MARKER
    text = text[:MAX_STRING_CHARS]
    return text if _IDENTIFIER_RE.match(text) else None


def _clean_int(value: object, maximum: int) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def _clean_float(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def sanitize_span(span: object) -> Optional[dict[str, Any]]:
    """Reduce ``span`` to its allowlisted, type-checked, body-free form.

    Returns ``None`` when the span is unusable (not a mapping, or no valid
    ``kind``/``status``). Unknown keys and values of the wrong type/shape are
    dropped silently. Never raises.
    """

    try:
        if not isinstance(span, Mapping):
            return None
        clean: dict[str, Any] = {}
        for key, kind in ALLOWED_TYPES.items():
            if key not in span:
                continue
            raw = span[key]
            value: Any
            if kind == _STR:
                value = _clean_string(raw)
            elif kind == _INT:
                maximum = _MAX_DURATION_MS if key == "duration_ms" else _MAX_COUNT
                value = _clean_int(raw, maximum)
            else:
                value = _clean_float(raw)
                if value is not None:
                    value = round(value, 6 if key == "ccc.cost_usd" else 3)
            if value is not None:
                clean[key] = value
        if clean.get("kind") not in SPAN_KINDS or clean.get("status") not in SPAN_STATUSES:
            return None
        return clean
    except Exception:
        return None


def iter_spans(path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield the spans of one day file, skipping corrupt/torn/foreign lines."""

    try:
        handle = Path(path).open("rb")
    except OSError:
        return
    with handle:
        for raw in handle:
            try:
                record = json.loads(raw)
            except Exception:
                continue
            if isinstance(record, dict) and record.get("kind") in SPAN_KINDS:
                yield record


def _utc_day(ts: float) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _pick_model(models: Any) -> Optional[str]:
    """The model that produced the most output in this turn (ties: first)."""

    best: Optional[str] = None
    best_key = (-1, -1)
    for entry in models or ():
        name = getattr(entry, "model", None)
        if not isinstance(name, str) or not name:
            continue
        key = (int(getattr(entry, "output_tokens", 0) or 0), int(getattr(entry, "input_tokens", 0) or 0))
        if key > best_key:
            best, best_key = name, key
    return best


def turn_span_from_claude_result(
    message: Any, delta: Any, *, end_ts: float
) -> dict[str, Any]:
    """Map one terminal Claude ``ResultMessage`` plus its per-turn usage delta
    (``delta_from_snapshots`` output, may be ``None``) to a raw ``turn`` span.

    Only ids, codes and numbers are read; ``message.result`` and the raw
    ``session_id`` are never copied (the session becomes an opaque ref).
    """

    is_error = bool(getattr(message, "is_error", False))
    subtype = getattr(message, "subtype", None)
    span: dict[str, Any] = {
        "kind": "turn",
        "status": "error" if is_error else "ok",
        "gen_ai.operation.name": OPERATION_INVOKE_AGENT,
        "ccc.outcome": subtype,
        "ccc.turn_id": getattr(message, "uuid", None),
        "gen_ai.conversation.id": conversation_ref(getattr(message, "session_id", None)),
    }
    if is_error:
        span["error.type"] = subtype if isinstance(subtype, str) and subtype else "error"
    duration = getattr(message, "duration_ms", None)
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        span["duration_ms"] = int(duration)
        span["start_ts"] = float(end_ts) - float(duration) / 1000.0
    else:
        span["start_ts"] = float(end_ts)
    if delta is not None:
        models = tuple(getattr(delta, "models", ()) or ())
        span["gen_ai.request.model"] = _pick_model(models)
        if models:
            cache_read = sum(int(getattr(m, "cache_read_input_tokens", 0) or 0) for m in models)
            cache_create = sum(int(getattr(m, "cache_creation_input_tokens", 0) or 0) for m in models)
            raw_input = sum(int(getattr(m, "input_tokens", 0) or 0) for m in models)
            span["gen_ai.usage.input_tokens"] = raw_input + cache_read + cache_create
            span["ccc.usage.cache_read_tokens"] = cache_read
            span["ccc.usage.cache_creation_tokens"] = cache_create
        elif getattr(delta, "input_tokens", None) is not None:
            span["gen_ai.usage.input_tokens"] = getattr(delta, "input_tokens")
        # Sum per-model output like input above and like the cost ledger does
        # (#2684 slice 4). The top-level ``usage.output_tokens`` is wrong here
        # twice over: it leaves out auxiliary/subagent models (haiku), and it
        # is not session-cumulative like ``model_usage``, so its snapshot delta
        # collapses to 0 on later turns of a session (gongmyoung 2026-11-07:
        # 51,279 span vs 137,598 ledger output tokens over 6 turns). Fall back
        # to it only when no model rows exist.
        output_tokens: Any
        if models:
            output_tokens = sum(int(getattr(m, "output_tokens", 0) or 0) for m in models)
        else:
            output_tokens = getattr(delta, "output_tokens", None)
        span["gen_ai.usage.output_tokens"] = output_tokens
        span["ccc.cost_usd"] = getattr(delta, "total_cost_usd", None)
    return span


class SpanLedger:
    """Append-only, body-free span ledger: one JSONL file per UTC day."""

    def __init__(
        self,
        root: Path | str,
        *,
        enabled: bool = True,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        clock: Callable[[], float] = time.time,
        node: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> None:
        self._root = Path(root)
        self._enabled = bool(enabled)
        try:
            days = int(retention_days)
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        self._retention_days = max(1, min(MAX_RETENTION_DAYS, days))
        self._clock = clock
        self._defaults = {"ccc.node": node, "ccc.channel": channel}
        self._pruned_day: Optional[date] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def root(self) -> Path:
        return self._root

    @property
    def retention_days(self) -> int:
        return self._retention_days

    def day_path(self, day: date | str) -> Path:
        name = day if isinstance(day, str) else day.isoformat()
        return self._root / f"{name}.jsonl"

    def read(self, day: date | str) -> Iterator[dict[str, Any]]:
        """Yield one day's spans, skipping corrupt lines. Never raises."""

        try:
            yield from iter_spans(self.day_path(day))
        except Exception:
            logger.debug("span ledger read failed", exc_info=True)

    def record(self, span: Mapping[str, Any]) -> bool:
        """Append one span; ``True`` when a line was written. Never raises."""

        if not self._enabled:
            return False
        try:
            now = float(self._clock())
            merged = {k: v for k, v in self._defaults.items() if v is not None}
            merged.update(span)
            clean = sanitize_span(merged)
            if clean is None:
                return False
            line = (json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(line) > MAX_LINE_BYTES:
                return False
            today = _utc_day(now)
            written = self._append(self.day_path(today), line)
            if self._pruned_day != today:
                # Mark first: a failing prune is retried tomorrow, not every turn.
                self._pruned_day = today
                self.prune(now=now)
            return written
        except Exception:
            logger.debug("span ledger append failed", exc_info=True)
            return False

    def _append(self, path: Path, line: bytes) -> bool:
        root = self._root
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except FileExistsError:
            pass
        if root.is_symlink() or not root.is_dir():
            return False
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        try:
            # One write of one complete line on O_APPEND: concurrent writers
            # (threads or a sibling frontend process) never interleave.
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)

    def prune(self, *, now: Optional[float] = None) -> int:
        """Delete day files past the retention window; return how many.

        Only regular files (``lstat``; symlinks are never followed or removed)
        named exactly ``YYYY-MM-DD.jsonl`` with a real date are candidates. A
        symlinked spans directory is refused outright. Never raises.
        """

        try:
            moment = float(self._clock()) if now is None else float(now)
            cutoff = _utc_day(moment) - timedelta(days=self._retention_days)
            root = self._root
            try:
                root_mode = os.lstat(root).st_mode
            except FileNotFoundError:
                return 0
            if not stat.S_ISDIR(root_mode):
                return 0
            removed = 0
            with os.scandir(root) as entries:
                for entry in entries:
                    match = SPAN_FILE_RE.match(entry.name)
                    if match is None:
                        continue
                    try:
                        day = date(int(match[1]), int(match[2]), int(match[3]))
                    except ValueError:
                        continue
                    if day > cutoff:
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        os.unlink(entry.path)
                        removed += 1
                    except OSError:
                        logger.debug("span ledger prune skipped %s", entry.name, exc_info=True)
            return removed
        except Exception:
            logger.debug("span ledger prune failed", exc_info=True)
            return 0


__all__ = [
    "ALLOWED_KEYS",
    "DEFAULT_RETENTION_DAYS",
    "SPAN_KINDS",
    "SPAN_STATUSES",
    "SpanLedger",
    "conversation_ref",
    "iter_spans",
    "sanitize_span",
    "turn_span_from_claude_result",
]
