#!/usr/bin/env python3
"""Hub-side static pages for the nightly span rollups (#2858, item 4).

Every node saves a body-free daily summary (``spans-rollup.py`` →
``<state>/spans-rollup/YYYY-MM-DD.json``). ``spans-view-collect.sh`` reads
that one file per node over a read-only stdin probe and stages the answers;
this script turns the staged answers into two kinds of static page:

    <out-dir>/YYYY-MM-DD.html   one section per node for that KST day
    <out-dir>/index.html        node x day table for the last 30 days

What it guarantees:

- **Nothing collected is trusted.** A staged answer is size-capped, must name
  schema ``ccc.spans-rollup.v1`` and the requested day, and is copied field by
  field into a new record: numbers must be finite numbers in range, labels are
  cut to a fixed length with control characters removed, lists are capped, and
  any key this script does not know is dropped. A known key with the wrong
  type rejects the whole answer.
- **A node that did not answer is shown, not hidden.** It appears as 「미수집」
  with the reason: no file for that day, schema mismatch, size cap, or the
  transport verdict the collector recorded (ssh 255 = UNREACHABLE, timeout 124
  = UNVERIFIED). An idle day (``"empty": true``) is collected data, not a gap.
- **Pages carry no script.** They are built by the fleet-html-report builder
  (inline CSS, every value HTML-escaped, ``default-src 'none'``); the only
  links are same-folder ``*.html`` names this script generates from dates.
- **The output folder is opened once and used only through that descriptor.**
  It must be a real directory (not a symlink) owned by this user or by the
  owner of its parent, and not group/other-writable; its parent path must hold
  no symlink. Pages are written to a temp name created with ``O_EXCL`` and
  renamed into place, so another local account that owns the parent cannot
  point a write, a scan or a delete somewhere else by swapping the folder.
- **One day at a time, anchored on today.** ``--date`` may not be later than
  yesterday (KST) or older than the retention window; the index and the
  90-day pruning always count back from yesterday, whatever day is rebuilt.

Raw spans never leave a node: the only input is the summary a node already
wrote for itself.

Usage:
    spans-view.py build --out-dir DIR [--date YYYY-MM-DD] [--nodes "a b c"]
                        [--staging DIR] [--data-dir DIR] [--keep-days 90]
                        [--index-days 30]

Without ``--staging`` the pages are rebuilt from the records already kept.
``CCC_SPANS_VIEW_NOW`` (an ISO timestamp) replaces the clock; it is a test seam.
Prints one ``SPANS_VIEW <STATE> <node> <detail>`` line per node and a final
JSON line. Exit 0 when the pages were written (missing nodes do not fail the
run), 2 on a usage or input error, 3 when a page could not be written.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SOURCE_SCHEMA = "ccc.spans-rollup.v1"
RECORD_SCHEMA = "ccc.spans-view-record.v1"
STATUS_SCHEMA = "ccc.spans-view-status.v1"
KST = timezone(timedelta(hours=9))  # Asia/Seoul has no DST; the rollup day is a KST day

MAX_STAGED_BYTES = 2 * 1024 * 1024  # one probe answer (ASCII-escaped JSON line + status lines)
MAX_RECORD_BYTES = 512 * 1024
MAX_LABEL = 120
MAX_TOOLS = 60
MAX_MODELS = 20
MAX_COUNTS = 30
MAX_INT = 10 ** 12
KEEP_DAYS = 90
INDEX_DAYS = 30
TOOL_ROWS = 15
MAX_SECTIONS = 38  # the builder refuses more than 40

ST_OK = "ok"
ST_EMPTY = "empty"
ST_MISSING = "missing"
ST_INVALID = "invalid"
ST_OVERSIZE = "oversize"
ST_UNREACHABLE = "unreachable"
ST_UNVERIFIED = "unverified"
COLLECTED = (ST_OK, ST_EMPTY)

_NODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DETAIL = re.compile(r"[^a-z0-9=._-]")

# Reason text shown next to 「미수집」. Keys are the detail codes produced below.
REASONS = {
    "no-folder": "집계 폴더 없음 — 노드 체크아웃이 집계 저장 이전 판이거나 아직 첫 롤업 전",
    "no-file": "그날 집계 파일 없음",
    "schema": "스키마 불일치 — 받은 내용을 버렸습니다",
    "date": "날짜 불일치 — 받은 내용을 버렸습니다",
    "not-json": "JSON이 아님 — 받은 내용을 버렸습니다",
    "unreadable": "노드에서 파일을 읽지 못함",
    "size": "크기 상한 초과 — 받은 내용을 버렸습니다",
    "ssh-255": "UNREACHABLE — ssh 연결 실패(255)",
    "timeout-124": "UNVERIFIED — 제한 시간 안에 응답 없음(124)",
    "probe-error": "UNVERIFIED — 프로브 오류",
    "incomplete-probe": "UNVERIFIED — 프로브가 끝까지 돌지 않음",
    "no-result": "UNVERIFIED — 수집 결과 없음",
}


class Invalid(ValueError):
    """A collected summary broke the contract; ``args[0]`` is the detail code."""


class UsageError(ValueError):
    pass


# ----------------------------------------------------------------- validation


def _int(value: Any, hi: int = MAX_INT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= hi:
        raise Invalid("schema")
    return value


def _num(value: Any, hi: float = 1e15, optional: bool = False) -> Optional[float]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid("schema")
    # Range first: it is exact for an int of any size and false for NaN/inf,
    # while math.isfinite() raises OverflowError on an int too large for a float.
    if not 0 <= value <= hi or not math.isfinite(value):
        raise Invalid("schema")
    return value


def _label(value: Any) -> str:
    if not isinstance(value, str):
        raise Invalid("schema")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value).strip()
    return cleaned[:MAX_LABEL] or "(이름 없음)"


def _mapping(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Invalid("schema")
    return value


def _counts(value: Any, cap: int = MAX_COUNTS) -> dict[str, int]:
    pairs = [(_label(key), _int(count)) for key, count in _mapping(value).items()]
    pairs.sort(key=lambda kv: (-kv[1], kv[0]))
    return dict(pairs[:cap])


def _tools(value: Any) -> list[dict]:
    rows = []
    for name, slot in _mapping(value).items():
        slot = _mapping(slot)
        rows.append({
            "name": _label(name),
            "calls": _int(slot.get("calls")),
            "errors": _int(slot.get("errors")),
            "error_rate_pct": _num(slot.get("error_rate_pct"), hi=100.0),
            "p50_ms": _num(slot.get("p50_ms"), optional=True),
            "p95_ms": _num(slot.get("p95_ms"), optional=True),
        })
    rows.sort(key=lambda row: (-row["calls"], row["name"]))
    return rows[:MAX_TOOLS]


def _models(value: Any) -> list[dict]:
    rows = []
    for name, slot in _mapping(value).items():
        slot = _mapping(slot)
        rows.append({
            "name": _label(name),
            "turns": _int(slot.get("turns")),
            "input_tokens": _int(slot.get("input_tokens")),
            "output_tokens": _int(slot.get("output_tokens")),
            "cache_read_tokens": _int(slot.get("cache_read_tokens", 0)),
            "cache_creation_tokens": _int(slot.get("cache_creation_tokens", 0)),
            "cost_usd": _num(slot.get("cost_usd")),
        })
    rows.sort(key=lambda row: (-row["turns"], row["name"]))
    return rows[:MAX_MODELS]


def _ledger(value: Any) -> dict:
    check = _mapping(value)
    if not check:
        return {"available": False}
    if not isinstance(check.get("available"), bool):
        raise Invalid("schema")
    if not check["available"]:
        return {"available": False}
    within = check.get("within_target")
    if not isinstance(within, bool):
        raise Invalid("schema")
    metrics = {}
    for name, slot in _mapping(check.get("metrics")).items():
        if name not in ("turns", "input_tokens", "output_tokens", "cost_usd"):
            continue
        slot = _mapping(slot)
        metrics[name] = {"spans": _num(slot.get("spans")), "ledger": _num(slot.get("ledger")),
                         "diff_pct": _num(slot.get("diff_pct"), optional=True)}
    return {
        "available": True,
        "within_target": within,
        "max_diff_pct": _num(check.get("max_diff_pct"), optional=True),
        "target_pct": _num(check.get("target_pct"), hi=100.0, optional=True),
        "zero_cost_rows": _int(check.get("ledger_zero_cost_rows", 0)),
        "metrics": metrics,
    }


def _a2a(value: Any) -> Optional[dict]:
    if value is None:
        return None
    slot = _mapping(value)
    return {"spans": _int(slot.get("spans")), "ok": _int(slot.get("ok")),
            "failed": _int(slot.get("failed")),
            "top_failure_classes": _counts(slot.get("top_failure_classes"), 10)}


def normalize_summary(raw: Any, day: str) -> dict:
    """Copy a collected rollup summary into a new, bounded record or raise ``Invalid``."""
    if not isinstance(raw, dict) or raw.get("schema") != SOURCE_SCHEMA:
        raise Invalid("schema")
    if raw.get("date") != day:
        raise Invalid("date")
    if not isinstance(raw.get("empty"), bool):
        raise Invalid("schema")
    totals = _mapping(raw.get("totals"))
    outcomes = _mapping(raw.get("turn_outcomes"))
    read = _mapping(raw.get("read"))
    return {
        "schema": RECORD_SCHEMA,
        "date": day,
        "node_reported": _label(raw.get("node")),
        "empty": raw["empty"],
        "totals": {
            "spans": _int(totals.get("spans")),
            "turns": _int(totals.get("turns")),
            "tool_calls": _int(totals.get("tool_calls")),
            "tool_errors": _int(totals.get("tool_errors")),
            "tool_error_rate_pct": _num(totals.get("tool_error_rate_pct", 0), hi=100.0),
        },
        "channels": _counts(totals.get("by_channel")),
        "kinds": _counts(totals.get("by_kind")),
        "tools": _tools(raw.get("tools")),
        "models": _models(raw.get("models")),
        "turn_status": _counts(outcomes.get("status")),
        "turn_outcome": _counts(outcomes.get("outcome")),
        "turn_error_type": _counts(outcomes.get("error_type")),
        "skills": _counts(raw.get("skills")),
        "ledger": _ledger(raw.get("cost_ledger_check")),
        "a2a": _a2a(raw.get("a2a")),
        "read": {key: _int(read.get(key, 0)) for key in
                 ("files_read", "corrupt_lines", "rejected_spans", "unreadable_files", "oversize_files")},
    }


# ------------------------------------------------------------------ file ops


def _read_regular(path: Path, limit: int) -> Optional[bytes]:
    """Bytes of a regular file no larger than ``limit``; ``None`` when absent.

    Raises ``Invalid("size")`` for a larger file and ``OSError`` for anything
    that is not a plain file (a symlink is never followed).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > limit:
            raise Invalid("size")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise Invalid("size")
    return data


def _read_json(path: Path, limit: int = MAX_RECORD_BYTES) -> Optional[dict]:
    try:
        data = _read_regular(path, limit)
        loaded = json.loads(data.decode("utf-8")) if data is not None else None
    except (OSError, ValueError, UnicodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _private_dir(path: Path) -> None:
    """Create one level of the hub's own record tree (0700); refuse a symlink."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str, mode: int) -> None:
    """Temp file + rename inside the hub's private record tree."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


class OutDir:
    """The page folder, opened once; every later step goes through its descriptor.

    The folder usually sits inside a web root that another local account owns.
    Working by path would let that account swap the folder for a symlink
    between two steps and aim a write, a listing or a delete elsewhere.
    """

    def __init__(self, path: Path):
        parent = os.path.abspath(path.parent)
        self.name = path.name
        if self.name in ("", ".", ".."):
            raise OSError("output folder needs a name")
        if os.path.realpath(parent) != parent:
            raise OSError("output folder: the parent path holds a symlink")
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parent_info = os.fstat(parent_fd)
            try:
                os.mkdir(self.name, 0o755, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                created = False
            self.fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            info = os.fstat(self.fd)
            if info.st_uid not in (os.geteuid(), parent_info.st_uid) or info.st_mode & 0o022:
                raise OSError("output folder: wrong owner or group/other-writable")
            if created:
                os.fchmod(self.fd, 0o755)  # the caller's umask may be private
        except OSError:
            os.close(self.fd)
            raise

    def close(self) -> None:
        os.close(self.fd)

    def write(self, name: str, text: str, mode: int = 0o644) -> None:
        tmp = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=self.fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp, dir_fd=self.fd)
                except OSError:
                    pass

    def is_page(self, name: str) -> bool:
        try:
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def prune(self, cutoff: date) -> int:
        """Remove dated day pages on or before ``cutoff``; leave every other name."""
        removed = 0
        for name in os.listdir(self.fd):
            stamp = _dated(name, ".html")
            if stamp is None or stamp > cutoff or not self.is_page(name):
                continue
            os.unlink(name, dir_fd=self.fd)
            removed += 1
        return removed


def _dated(name: str, suffix: str) -> Optional[date]:
    if not name.endswith(suffix) or not _DAY.match(name[: len(name) - len(suffix)]):
        return None
    try:
        return datetime.strptime(name[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def prune_records(data_dir: Path, cutoff: date) -> int:
    """Remove dated record folders on or before ``cutoff``; return how many."""
    removed = 0
    for entry in os.scandir(data_dir):
        stamp = _dated(entry.name, "")
        if stamp is None or stamp > cutoff or not entry.is_dir(follow_symlinks=False):
            continue
        for inner in os.scandir(entry.path):
            if inner.is_file(follow_symlinks=False) and inner.name.endswith(".json"):
                os.unlink(inner.path)
        try:
            os.rmdir(entry.path)
            removed += 1
        except OSError:
            pass  # something unexpected is inside; leave the folder alone
    return removed


# ------------------------------------------------------------------- ingest

_TRANSPORT = {
    "unreachable": (ST_UNREACHABLE, "ssh-255"),
    "timeout": (ST_UNVERIFIED, "timeout-124"),
    "oversize": (ST_OVERSIZE, "size"),
    "error": (ST_UNVERIFIED, "probe-error"),
}
_PROBE_STATUS = {
    "oversize": (ST_OVERSIZE, "size"),
    "unreadable": (ST_INVALID, "unreadable"),
    "not-json": (ST_INVALID, "not-json"),
}


def _probe_fields(text: str) -> Optional[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[-1] != "PROBE_COMPLETE=1":
        return None
    fields: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.startswith("SPANS_VIEW_") and key not in fields:
            fields[key] = value
    return fields


def _from_probe(text: str, day: str) -> tuple[str, str, Optional[dict]]:
    fields = _probe_fields(text)
    if fields is None:
        return ST_UNVERIFIED, "incomplete-probe", None
    status = fields.get("SPANS_VIEW_STATUS", "")
    if status == "missing":
        return ST_MISSING, "no-file" if fields.get("SPANS_VIEW_FOLDER") == "1" else "no-folder", None
    if status in _PROBE_STATUS:
        return (*_PROBE_STATUS[status], None)
    if status != "ok":
        return ST_UNVERIFIED, "probe-error", None
    try:
        record = normalize_summary(json.loads(fields.get("SPANS_VIEW_JSON", "")), day)
    except Invalid as exc:
        return ST_INVALID, str(exc.args[0]), None
    except (ValueError, RecursionError):
        return ST_INVALID, "not-json", None
    return (ST_EMPTY if record["empty"] else ST_OK), "", record


def read_staged(staging: Path, node: str, day: str) -> tuple[str, str, Optional[dict]]:
    """``(state, detail, record)`` for one node from the collector's staging folder."""
    try:
        meta = _read_regular(staging / f"{node}.meta", 256)
    except (OSError, Invalid):
        meta = None
    if meta is None:
        return ST_UNVERIFIED, "no-result", None
    words = dict(part.partition("=")[::2] for part in meta.decode("utf-8", "replace").split())
    transport = words.get("transport", "")
    if transport in _TRANSPORT:
        return (*_TRANSPORT[transport], None)
    if transport != "ok":
        return ST_UNVERIFIED, "probe-error", None
    try:
        raw = _read_regular(staging / f"{node}.out", MAX_STAGED_BYTES)
    except Invalid:
        return ST_OVERSIZE, "size", None
    except OSError:
        return ST_UNVERIFIED, "no-result", None
    if raw is None:
        return ST_UNVERIFIED, "no-result", None
    return _from_probe(raw.decode("utf-8", "replace"), day)


def load_status(data_dir: Path, day: str) -> dict[str, dict]:
    loaded = _read_json(data_dir / day / "_status.json")
    nodes = loaded.get("nodes") if loaded and loaded.get("schema") == STATUS_SCHEMA else None
    clean: dict[str, dict] = {}
    for node, slot in (nodes if isinstance(nodes, dict) else {}).items():
        if isinstance(node, str) and _NODE.match(node) and isinstance(slot, dict):
            state = slot.get("state")
            detail = slot.get("detail")
            if isinstance(state, str) and isinstance(detail, str):
                clean[node] = {"state": state, "detail": _DETAIL.sub("", detail)[:40]}
    return clean


def ingest(staging: Path, data_dir: Path, day: str, nodes: list[str]) -> dict[str, dict]:
    """Fold the staged answers into the day's records; return the day's status."""
    day_dir = data_dir / day
    _private_dir(day_dir)
    status = load_status(data_dir, day)
    for node in nodes:
        try:
            state, detail, record = read_staged(staging, node, day)
        except Exception:  # whatever one answer does, it costs that node only
            state, detail, record = ST_INVALID, "schema", None
        if record is None:
            # A failed re-run never replaces a record an earlier run collected.
            if status.get(node, {}).get("state") in COLLECTED:
                continue
            status[node] = {"state": state, "detail": detail}
            continue
        _atomic_write(day_dir / f"{node}.json",
                      json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", 0o600)
        status[node] = {"state": state, "detail": ""}
    payload = {"schema": STATUS_SCHEMA, "date": day, "nodes": status,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _atomic_write(day_dir / "_status.json",
                  json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", 0o600)
    return status


def load_record(data_dir: Path, day: str, node: str) -> Optional[dict]:
    record = _read_json(data_dir / day / f"{node}.json")
    if record is None or record.get("schema") != RECORD_SCHEMA or record.get("date") != day:
        return None
    return record


# ---------------------------------------------------------------- rendering


def _n(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "-"


def _usd(value: Any) -> str:
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "-"


def _pct(value: Any) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else "-"


def _ms(value: Any) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "-"


def _joined(counts: dict[str, int], cap: int = 8) -> str:
    return " · ".join(f"{name} {count:,}" for name, count in list(counts.items())[:cap]) or "-"


def _cost(record: dict) -> float:
    return round(sum(float(model.get("cost_usd") or 0) for model in record.get("models", [])), 6)


def _ledger_text(ledger: dict) -> str:
    if not ledger.get("available"):
        return "원장 없음"
    worst = ledger.get("max_diff_pct")
    verdict = "기준 내" if ledger.get("within_target") else "기준 초과"
    return f"{_pct(worst)} ({verdict})" if worst is not None else f"비교 불가 ({verdict})"


def _a2a_text(a2a: Optional[dict]) -> str:
    if not a2a:
        return "-"
    return f"{a2a['spans']:,}건 · 실패 {a2a['failed']:,}"


def _missing_text(slot: Optional[dict]) -> str:
    if slot is None:
        return "미수집 — " + REASONS["no-result"]
    return "미수집 — " + REASONS.get(slot.get("detail", ""), "사유 미상")


def _summary_row(node: str, slot: Optional[dict], record: Optional[dict]) -> list[str]:
    state = (slot or {}).get("state")
    if state == ST_EMPTY and record is not None:
        return [node, "활동 없음", "0", "0", "-", "-", "-", "-"]
    if state == ST_OK and record is not None:
        totals = record["totals"]
        return [node, "수집", _n(totals["turns"]), _n(totals["tool_calls"]),
                _pct(totals["tool_error_rate_pct"]), _usd(_cost(record)),
                _ledger_text(record["ledger"]), _a2a_text(record.get("a2a"))]
    return [node, _missing_text(slot), "-", "-", "-", "-", "-", "-"]


def _node_items(node: str, record: dict) -> list[list[str]]:
    totals = record["totals"]
    items = [["스팬", f"{totals['spans']:,}개 — 턴 {totals['turns']:,} · 도구 호출 "
                      f"{totals['tool_calls']:,} (오류 {totals['tool_errors']:,}, "
                      f"{_pct(totals['tool_error_rate_pct'])})"],
             ["채널", _joined(record["channels"])]]
    for model in record["models"]:
        items.append([f"모델 {model['name']}",
                      f"턴 {model['turns']:,} · 입력 {model['input_tokens']:,} · 출력 "
                      f"{model['output_tokens']:,} · 캐시 읽기 {model['cache_read_tokens']:,} · "
                      f"비용 {_usd(model['cost_usd'])}"])
    items.append(["턴 결과", _joined(record["turn_status"])])
    if record["turn_error_type"]:
        items.append(["오류 유형", _joined(record["turn_error_type"])])
    if record["skills"]:
        items.append(["스킬", _joined(record["skills"], 10)])
    ledger = record["ledger"]
    text = _ledger_text(ledger)
    if ledger.get("available"):
        text += f" · 비용 0 원장 줄 {ledger['zero_cost_rows']:,}"
    items.append(["원장 교차검증", text])
    if record.get("a2a"):
        items.append(["a2a", _a2a_text(record["a2a"]) + " · "
                      + _joined(record["a2a"]["top_failure_classes"], 5)])
    read = record["read"]
    items.append(["읽기", f"파일 {read['files_read']:,} · 손상 줄 {read['corrupt_lines']:,} · "
                          f"거부 스팬 {read['rejected_spans']:,}"])
    if record["node_reported"] != node:
        items.append(["노드가 적은 이름", record["node_reported"]])
    return items


def _node_sections(node: str, slot: Optional[dict], record: Optional[dict], room: int) -> list[dict]:
    title = f"노드 {node}"
    state = (slot or {}).get("state")
    if record is None or state not in COLLECTED:
        return [{"kind": "text", "title": title, "text": _missing_text(slot)}]
    if state == ST_EMPTY:
        return [{"kind": "text", "title": title,
                 "text": "활동 없음 — 이 날은 스팬이 없습니다(집계 파일은 수집했습니다)."}]
    sections: list[dict] = [{"kind": "kv", "title": title, "items": _node_items(node, record)}]
    if record["tools"] and room >= 2:
        rows = [[tool["name"], _n(tool["calls"]), _n(tool["errors"]), _pct(tool["error_rate_pct"]),
                 _ms(tool["p50_ms"]), _ms(tool["p95_ms"])] for tool in record["tools"][:TOOL_ROWS]]
        sections.append({"kind": "table", "title": f"노드 {node} — 도구 (호출 상위 {len(rows)})",
                         "columns": ["도구", "호출", "오류", "오류율", "p50 ms", "p95 ms"], "rows": rows})
    return sections


def _total_row(records: list[dict]) -> list[str]:
    turns = sum(record["totals"]["turns"] for record in records)
    calls = sum(record["totals"]["tool_calls"] for record in records)
    errors = sum(record["totals"]["tool_errors"] for record in records)
    rate = round(100.0 * errors / calls, 2) if calls else None
    return ["합계", f"수집 {len(records)}", _n(turns), _n(calls), _pct(rate),
            _usd(sum(_cost(record) for record in records)), "-", "-"]


def day_spec(day: str, nodes: list[str], status: dict[str, dict],
             records: dict[str, dict], generated: str) -> dict:
    collected = [node for node in nodes
                 if status.get(node, {}).get("state") in COLLECTED and node in records]
    rows = [_summary_row(node, status.get(node), records.get(node)) for node in nodes]
    rows.append(_total_row([records[node] for node in collected]))
    sections: list[dict] = [{
        "kind": "table", "title": "노드 요약",
        "columns": ["노드", "상태", "턴", "도구 호출", "도구 오류율", "비용(USD)", "원장 차이", "a2a"],
        "rows": rows}]
    for index, node in enumerate(nodes):
        # Every later node still needs one section; a tool table is the extra one.
        room = MAX_SECTIONS - len(sections) - (len(nodes) - index - 1)
        if room < 1:
            break
        sections += _node_sections(node, status.get(node), records.get(node), room)
    return {
        "title": f"스팬 롤업 — {day}",
        "subtitle": f"생성 {generated} · 수집 {len(collected)}/{len(nodes)} 노드 · "
                    "본문 없음(숫자와 이름만)",
        "sections": sections,
        "footer": "ccc-node spans-view · 스팬 원본은 각 노드에만 있습니다 · index.html 에서 다른 날짜",
    }


def _index_cell(slot: Optional[dict], record: Optional[dict]) -> str:
    if slot is None:
        return "–"
    if slot["state"] == ST_EMPTY and record is not None:
        return "0"
    if slot["state"] == ST_OK and record is not None:
        return f"{record['totals']['turns']:,}턴 · {_usd(_cost(record))}"
    return "미수집"


def index_spec(day: date, nodes: list[str], data_dir: Path, out: OutDir,
               days: int, generated: str) -> dict:
    rows = []
    for offset in range(days):
        name = (day - timedelta(days=offset)).isoformat()
        status = load_status(data_dir, name)
        linked = out.is_page(f"{name}.html")
        first: Any = {"text": name, "href": f"{name}.html"} if linked else name
        row = [first]
        for node in nodes:
            slot = status.get(node) if status else None
            record = load_record(data_dir, name, node) if slot and slot["state"] in COLLECTED else None
            row.append(_index_cell(slot, record))
        rows.append(row)
    return {
        "title": f"스팬 롤업 — 최근 {days}일",
        "subtitle": f"생성 {generated} · 최근 날짜 {day.isoformat()} · 본문 없음(숫자와 이름만)",
        "sections": [
            {"kind": "table", "title": "노드 × 날짜 (턴 수 · 비용)", "columns": ["날짜", *nodes],
             "rows": rows},
            {"kind": "list", "title": "읽는 법", "items": [
                "N턴 · $비용 — 그날 집계를 수집했습니다. 날짜를 누르면 노드별 상세가 열립니다.",
                "0 — 수집했고 그날 활동이 없었습니다.",
                "미수집 — 그날 그 노드의 집계를 읽지 못했습니다. 사유는 날짜 페이지에 있습니다.",
                "– — 허브가 그날 수집을 돌리지 않았습니다.",
                "대화 본문·프롬프트·도구 입력은 수집하지 않습니다. 스팬 원본은 각 노드에만 있습니다.",
            ]},
        ],
        "footer": "ccc-node spans-view",
    }


def load_builder():
    path = (Path(__file__).resolve().parents[1]
            / "skills/fleet-html-report/scripts/build_html_report.py")
    spec = importlib.util.spec_from_file_location("ccc_fleet_html_report", path)
    if spec is None or spec.loader is None:
        raise UsageError("fleet-html-report builder is unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except OSError as exc:
        raise UsageError("fleet-html-report builder is unavailable") from exc
    return module


# ---------------------------------------------------------------------- main


def parse_nodes(text: str) -> list[str]:
    nodes: list[str] = []
    for name in text.split():
        if not _NODE.match(name):
            raise UsageError("node names must be plain names (letters, digits, . _ -)")
        if name not in nodes:
            nodes.append(name)
    if not nodes:
        raise UsageError("no nodes given (--nodes or CCC_FLEET_NODES)")
    return nodes


def clock() -> datetime:
    """Now, or ``CCC_SPANS_VIEW_NOW`` (ISO 8601 with offset) — a test seam."""
    text = os.environ.get("CCC_SPANS_VIEW_NOW")
    if not text:
        return datetime.now(timezone.utc)
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise UsageError("bad CCC_SPANS_VIEW_NOW") from exc
    if value.tzinfo is None:
        raise UsageError("bad CCC_SPANS_VIEW_NOW")
    return value


def parse_day(text: Optional[str], now: datetime, keep_days: int = KEEP_DAYS) -> date:
    """The KST day to build. Never later than yesterday, never past retention.

    A mistyped year must not be able to prune everything (the window counts
    back from yesterday, see ``build``), and a day already outside the window
    would be written and removed in the same run.
    """
    anchor = (now.astimezone(KST) - timedelta(days=1)).date()
    if not text:
        return anchor
    if not _DAY.match(text):
        raise UsageError("bad --date (want YYYY-MM-DD)")
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise UsageError("bad --date (want YYYY-MM-DD)") from exc
    if day > anchor:
        raise UsageError("--date is later than yesterday (KST)")
    if day <= anchor - timedelta(days=keep_days):
        raise UsageError("--date is older than the retention window")
    return day


def _records_root(args: argparse.Namespace) -> Path:
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        state = Path(os.environ.get("CCC_STATE_DIR") or Path.home() / ".claude" / "state")
        _private_dir(state / "spans-view")
        data_dir = state / "spans-view" / "data"
    _private_dir(data_dir)
    return data_dir


def build(args: argparse.Namespace, now: datetime) -> dict:
    nodes = parse_nodes(args.nodes or os.environ.get("CCC_FLEET_NODES", ""))
    if not 1 <= args.keep_days <= 3650 or not 1 <= args.index_days <= 366:
        raise UsageError("--keep-days / --index-days out of range")
    day = parse_day(args.date, now, args.keep_days)
    anchor = (now.astimezone(KST) - timedelta(days=1)).date()
    builder = load_builder()
    out = OutDir(Path(args.out_dir))
    try:
        data_dir = _records_root(args)
        name = day.isoformat()
        if args.staging:
            status = ingest(Path(args.staging), data_dir, name, nodes)
        else:
            status = load_status(data_dir, name)
        records = {}
        for node in nodes:
            record = load_record(data_dir, name, node)
            if record is not None:
                records[node] = record
        generated = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        out.write(f"{name}.html", builder.build_html(day_spec(name, nodes, status, records, generated)))
        # Retention and the index count back from yesterday, not from --date:
        # rebuilding an old day must not drop newer pages or shift the table.
        cutoff = anchor - timedelta(days=args.keep_days)
        removed = out.prune(cutoff) + prune_records(data_dir, cutoff)
        out.write("index.html", builder.build_html(
            index_spec(anchor, nodes, data_dir, out, args.index_days, generated)))
    finally:
        out.close()
    for node in nodes:
        slot = status.get(node) or {"state": ST_UNVERIFIED, "detail": "no-result"}
        print(f"SPANS_VIEW {slot['state'].upper()} {node} {slot['detail']}".rstrip())
    collected = [n for n in nodes if status.get(n, {}).get("state") in COLLECTED and n in records]
    return {"ok": True, "date": name, "nodes": len(nodes), "collected": len(collected),
            "not_collected": [n for n in nodes if n not in collected],
            "pages": [f"{name}.html", "index.html"], "pruned": removed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Static span-rollup pages for the hub (#2858).")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("build", help="ingest staged probe answers (optional) and write the pages")
    cmd.add_argument("--out-dir", required=True, help="folder that receives the *.html pages")
    cmd.add_argument("--date", help="KST day YYYY-MM-DD (default: yesterday)")
    cmd.add_argument("--nodes", help="space-separated node names (default: $CCC_FLEET_NODES)")
    cmd.add_argument("--staging", help="collector staging folder (<node>.meta / <node>.out)")
    cmd.add_argument("--data-dir", help="record folder (default: $CCC_STATE_DIR/spans-view/data)")
    cmd.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    cmd.add_argument("--index-days", type=int, default=INDEX_DAYS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build(args, clock())
    except UsageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"write failed ({type(exc).__name__})"}))
        return 3
    except Exception as exc:  # the builder's SpecError and anything unforeseen: no traceback, no page
        print(json.dumps({"ok": False, "error": f"build failed ({type(exc).__name__})"}))
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
