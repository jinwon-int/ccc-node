#!/usr/bin/env python3
"""auto-distill → nunchi feed (#2186).

auto-distill (scripts/auto-distill/auto-distill.py, cron) extracts evidence-
backed operational facts from session transcripts and runs them through its
structure / value / canon-dedup / entailment gates. Until #2186 the survivors
only landed in a node-local AUTO.md that waited for a human verdict that never
came (2026-10-08: ~920 Wiki items, 57 judged; daegyo 952 local, 0 judged; no
runtime reader at all). This feed hands the gate survivors to nunchi instead,
where the session-start `assemble` injection already reaches every agent and
nunchi's own machinery (G1-G5, judge-batch, live-check marker, TTL) replaces
the human review.

What it reads: the extractor's structured per-run log
(`~/.hermes/logs/auto-distill-dryrun.jsonl`, one JSON record per processed
session: `session`, `path`, `kept[]`, `quarantined[]`, ...). Only `kept[]` is
fed — `quarantined[]` failed the entailment gate and never reaches agents.
auto-distill.py itself is untouched, so its exact-source evaluation receipt
stays valid.

How it feeds: one `nunchi.py ingest -` payload per record, tagged
`evidence_source: "auto-distill"` so nunchi labels the rows and caps their
share of the injection budget. Kinds map onto nunchi's taxonomy:
config / incident / decision → `context` (live-check, injected with ⟳ — the
items carry no structured reason, so `decision` would only fill the G5 review
queue), runbook → `procedure`. Text and quote are secret-redacted with the
same patterns auto-distill uses (claude/hooks/jev/jevlib/redact.py); when that
module is missing the feed refuses to run rather than ingest unredacted text.

Progress is a byte offset into the log (state file, 0600, atomic replace),
advanced only past records whose ingest succeeded. auto-distill only appends
to this log; a rotated or truncated log (inode change or shorter than the
offset) is read from its start — a fresh log holds only new records, and a log
rewritten with old content is re-fed once, which nunchi's dedup absorbs for
rows already stored. The first run starts at the end of the log unless
`--backfill-days N` asks for records whose transcript changed in the last N
days.

Opt-in per node: runs only when NUNCHI_AUTO_DISTILL_FEED=1 or
`$CCC_STATE_DIR/nunchi.auto-distill-feed` contains `on`. Never runs in
audience-scoped mode (#1921): auto-distill facts come from every session on
the node and must not be written into a shared store that is partitioned by
audience.

Exit codes: 0 ok / disabled / nothing to do, 2 refused (redaction module
missing, unsafe path), 3 an ingest failed (offset kept before that record).
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
redact_fn = None  # set by run() once the redaction module has loaded
KIND_MAP = {
    "config": "context",
    "incident": "context",
    "decision": "context",
    "runbook": "procedure",
}
TEXT_MAX = 600
QUOTE_MAX = 260
MAX_RECORDS_DEFAULT = 50
# A record that fails ingest this many ticks in a row is skipped (logged), so
# one poison record cannot block every later record forever.
POISON_AFTER = 3


def _redactor():
    sys.path.insert(0, str(HERE.parent / "jev"))
    try:
        from jevlib.redact import redact_text  # type: ignore
    except Exception:  # noqa: BLE001 — any import failure means "no redaction"
        return None
    finally:
        sys.path.pop(0)
    return redact_text


def enabled(state_dir: Path) -> bool:
    if os.environ.get("NUNCHI_AUTO_DISTILL_FEED") == "1":
        return True
    marker = state_dir / "nunchi.auto-distill-feed"
    try:
        return marker.read_text(encoding="utf-8").strip() == "on"
    except OSError:
        return False


def _first_quote(raw) -> str:
    """auto-distill stores the cited text as a stringified list; take entry 0."""
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw or "")
        try:
            parsed = ast.literal_eval(text)
            items = parsed if isinstance(parsed, list) else [text]
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            items = [text]
    for item in items:
        quote = " ".join(str(item).split())
        if quote:
            return quote
    return ""


def payload_for(record: dict, redact) -> dict | None:
    """One nunchi ingest payload for a run record, or None when nothing kept."""
    kept = record.get("kept")
    if not isinstance(kept, list) or not kept:
        return None
    items = []
    for item in kept:
        if not isinstance(item, dict):
            continue
        fact = " ".join(str(item.get("fact") or "").split())
        if not fact:
            continue
        title = " ".join(str(item.get("title") or "").split())
        text = f"{title} — {fact}" if title and title not in fact else fact
        kind = KIND_MAP.get(str(item.get("kind") or "").lower(), "context")
        entry = {
            "kind": kind,
            "subject": "node",
            "text": redact(text)[:TEXT_MAX],
        }
        quote = _first_quote(item.get("_evidence_text"))
        if quote:
            entry["evidence"] = redact(quote)[:QUOTE_MAX]
        items.append(entry)
    if not items:
        return None
    payload = {
        "session_id": str(record.get("session") or "unknown"),
        "honcho": items,
        "evidence_source": "auto-distill",
    }
    # valid_from = when the session last changed, not when the feed ran, so a
    # backfilled row does not look newer than it is.
    path = record.get("path")
    try:
        mtime = os.path.getmtime(path) if isinstance(path, str) and path else None
    except OSError:
        mtime = None
    if mtime is not None:
        payload["distilled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
    return payload


def _load_state(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"state file is a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".auto-distill-feed.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _recent(record: dict, cutoff: float) -> bool:
    path = record.get("path")
    try:
        return isinstance(path, str) and os.path.getmtime(path) >= cutoff
    except OSError:
        return False


def _ingest(nunchi_py: Path, payload: dict) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, str(nunchi_py), "ingest", "-"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _locked(lock_path: Path):
    """Non-blocking exclusive lock; None when another feed run holds it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def run(args) -> int:
    state_dir = Path(args.state_dir)
    if not enabled(state_dir):
        return 0
    if os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPED", "0") == "1":
        print("auto-distill-feed: skipped (audience-scoped mode, #1921)")
        return 0
    global redact_fn
    redact_fn = _redactor()
    if redact_fn is None:
        print("auto-distill-feed: refused — jevlib/redact.py missing beside the "
              "nunchi hooks; re-run setup.sh (never ingest unredacted text)", file=sys.stderr)
        return 2
    log = Path(args.log)
    state_path = Path(args.state)
    if log.is_symlink():
        print(f"auto-distill-feed: refused — log is a symlink: {log}", file=sys.stderr)
        return 2
    # Own lock (not ingest-cron's, which the cron caller already holds): a
    # manual backfill and a cron tick must never feed the same records twice.
    lock = _locked(state_path.parent / ".auto-distill-feed.lock")
    if lock is None:
        print("auto-distill-feed: another run holds the lock — skipping")
        return 0
    try:
        return _run_locked(args, log, state_path)
    finally:
        lock.close()


def _start(args, st, state: dict, state_path: Path):
    """(offset, cutoff, fail_count) to read from, or None after initialising."""
    try:
        offset = int(state["offset"])
        first_run = offset < 0
    except (KeyError, TypeError, ValueError):
        offset, first_run = 0, True  # absent or corrupt state: start fresh
    if not first_run and args.backfill_days is not None:
        print("auto-distill-feed: --backfill-days ignored — already initialised "
              f"(offset={offset}); remove {state_path} first to backfill")
    if state.get("inode") != st.st_ino or offset > st.st_size:
        offset = 0  # rotated/truncated: a fresh log holds only new records
    if first_run and args.backfill_days is None:
        _save_state(state_path, {"inode": st.st_ino, "offset": st.st_size,
                                 "updated_at": int(time.time())})
        print(f"auto-distill-feed: initialised at end of log (offset={st.st_size}); "
              "use --backfill-days N to feed older records")
        return None
    # A backfill cutoff is kept in the state until the backlog reaches EOF, so
    # a backfill larger than --max-records stays bounded on later ticks too.
    if first_run:
        return 0, time.time() - args.backfill_days * 86400, 0
    fail_count = int(state.get("fail_count") or 0) if state.get("fail_offset") == offset else 0
    return offset, state.get("backfill_cutoff"), fail_count


class _Progress:
    """Offset + counters of one tick, persisted after every consumed record."""

    def __init__(self, st, state_path: Path, offset: int, cutoff, fail_count: int):
        self.st, self.state_path = st, state_path
        self.offset, self.cutoff, self.fail_count = offset, cutoff, fail_count
        self.records = self.fed_records = self.items = 0

    def save(self, at_end: bool) -> None:
        data = {"inode": self.st.st_ino, "offset": self.offset,
                "updated_at": int(time.time()),
                "last_run": {"records": self.records, "fed_records": self.fed_records,
                             "items": self.items}}
        if self.cutoff is not None and not at_end:
            data["backfill_cutoff"] = self.cutoff
        if self.fail_count:
            data["fail_offset"], data["fail_count"] = self.offset, self.fail_count
        _save_state(self.state_path, data)


def _consume(args, prog: _Progress, line: bytes) -> bool:
    """Feed one complete log line; False when ingest failed and must be retried."""
    try:
        record = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        record = None
    payload = (payload_for(record, redact_fn)
               if isinstance(record, dict) and (prog.cutoff is None or _recent(record, prog.cutoff))
               else None)
    if payload is None:
        return True
    if _ingest(Path(args.nunchi_py), payload):
        prog.fed_records += 1
        prog.items += len(payload["honcho"])
        return True
    prog.fail_count += 1
    if prog.fail_count < POISON_AFTER:
        return False
    print(f"auto-distill-feed: skipping record at offset {prog.offset} after "
          f"{prog.fail_count} failed ingests", file=sys.stderr)
    return True


def _run_locked(args, log: Path, state_path: Path) -> int:
    try:
        st = log.stat()
        state = _load_state(state_path)
    except FileNotFoundError:
        return 0  # auto-distill not installed or never ran on this node
    except ValueError as exc:
        print(f"auto-distill-feed: refused — {exc}", file=sys.stderr)
        return 2
    start = _start(args, st, state, state_path)
    if start is None:
        return 0
    prog = _Progress(st, state_path, *start)
    rc = 0
    at_eof = False
    with open(log, "rb") as fh:
        fh.seek(prog.offset)
        while prog.records < args.max_records:
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                at_eof = True  # EOF, or a record still being written
                break
            next_offset = fh.tell()
            prog.records += 1
            if not _consume(args, prog, line):
                rc = 3
                break
            prog.fail_count = 0
            prog.offset = next_offset
            prog.save(False)
        else:
            at_eof = fh.tell() >= st.st_size
    prog.save(at_eof and rc == 0)
    if prog.records or rc:
        print(f"auto-distill-feed: records={prog.records} fed_records={prog.fed_records} "
              f"items={prog.items} offset={prog.offset}" + (" ingest-failed" if rc else ""))
    return rc


def main(argv=None) -> int:
    home = Path.home()
    nunchi_home = Path(os.environ.get("NUNCHI_HOME") or home / ".nunchi")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--log", default=os.environ.get(
        "NUNCHI_AUTO_DISTILL_LOG", str(home / ".hermes/logs/auto-distill-dryrun.jsonl")))
    ap.add_argument("--state", default=str(nunchi_home / "auto-distill-feed.state.json"))
    ap.add_argument("--state-dir", default=os.environ.get(
        "CCC_STATE_DIR", str(home / ".claude/state")))
    ap.add_argument("--nunchi-py", default=str(HERE / "nunchi.py"))
    ap.add_argument("--max-records", type=int, default=MAX_RECORDS_DEFAULT)
    ap.add_argument("--backfill-days", type=int, default=None,
                    help="first run only: feed records whose transcript changed in the last N days")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
