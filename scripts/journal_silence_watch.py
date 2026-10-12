#!/usr/bin/env python3
"""Page when a systemd unit's journal stops showing an expected line.

A webhook receiver can stay perfectly healthy (unit active, HTTP 200 on its
docs page) while the *sender* silently stops posting: a phone automation app
whose task queue hangs keeps its process alive, so nothing on the server side
ever fails. The only symptom is absence — no request lines in the receiver's
journal. This watcher turns that absence into an owner page.

It reads the unit's journal (``journalctl -u UNIT -o short-unix``), finds the
newest line matching ``--pattern``, and compares its age with
``--max-silence-min``. Paging is change-based through ``--state-file`` so a
long outage does not page every run:

- ``NEW``        the unit went silent (or the journal became unreadable);
- ``STILL``      still silent, at most once per ``--still-every-min``;
- ``RECOVERED``  a matching line appeared again (once, with the outage span).

Exit codes: ``0`` nothing to page, ``1`` page (the stdout text is the page),
``2`` usage error. Run it as an agent-cron ``command`` task with
``--notify telegram-owner-on-failure`` and ``--failure-alert-after 0``: exit 1
is the page, so the generic consecutive-failure alarm must stay off.

Only timestamps and counts are printed — never the matched journal text, which
for a notification relay can carry message bodies or payment details.

Usage:
  journal_silence_watch.py --unit UNIT --pattern REGEX --state-file PATH
      [--max-silence-min 30] [--still-every-min 180] [--lookback-hours 72]
      [--label TEXT] [--tz Asia/Seoul] [--now EPOCH]

``CCC_JOURNAL_SILENCE_JOURNALCTL`` overrides the journalctl binary (tests).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = "ccc.journal-silence-watch.v1"
JOURNAL_TIMEOUT_SEC = 60


class JournalError(Exception):
    """The journal could not be read; the verdict is UNVERIFIED, not silence."""


def read_last_match(
    journalctl: str, unit: str, pattern: re.Pattern[str], since_epoch: float,
    until_epoch: float,
) -> tuple[float | None, int]:
    """Return (newest matching line epoch or None, number of matching lines).

    Lines after ``until_epoch`` are ignored, so ``--now`` in the past replays
    that moment instead of seeing requests that arrived later.
    """
    cmd = [
        journalctl, "-u", unit, "--since", f"@{int(since_epoch)}",
        "--until", f"@{int(until_epoch) + 1}",
        "-o", "short-unix", "--no-pager", "-q",
    ]
    try:
        proc = subprocess.run(
            cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL,
            check=False, timeout=JOURNAL_TIMEOUT_SEC, errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise JournalError(f"journalctl timed out after {JOURNAL_TIMEOUT_SEC}s") from exc
    except OSError as exc:
        raise JournalError(f"journalctl failed to start: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        raise JournalError(f"journalctl exit {proc.returncode}")
    last: float | None = None
    count = 0
    for line in proc.stdout.splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            ts = float(stamp)
        except ValueError:
            continue
        if ts > until_epoch:
            continue
        if pattern.search(rest):
            count += 1
            if last is None or ts > last:
                last = ts
    return last, count


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".jsw-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with_suppressed_unlink(tmp)
        raise


def with_suppressed_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def fmt_ts(epoch: float | None, tz: ZoneInfo) -> str:
    if epoch is None:
        return "-"
    return dt.datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_span(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def decide(
    prev: dict[str, Any], cur: str, now: float, still_every: float
) -> tuple[str | None, dict[str, Any]]:
    """Fold the current verdict into the previous state.

    Returns (page kind or None, next state fields). ``cur`` is ``ok``,
    ``silent`` or ``unverified``; a missing previous state counts as ``ok`` so
    a watcher installed during an outage pages on its first run.
    """
    prev_state = prev.get("state", "ok")
    since = prev.get("since", now)
    last_page = prev.get("lastPageAt")
    if cur == "ok":
        if prev_state != "ok":
            return "RECOVERED", {"state": "ok", "since": now, "lastPageAt": now,
                                 "outageSince": since}
        return None, {"state": "ok", "since": since, "lastPageAt": last_page}
    if prev_state != cur:
        return "NEW", {"state": cur, "since": now, "lastPageAt": now}
    if last_page is None or now - float(last_page) >= still_every:
        return "STILL", {"state": cur, "since": since, "lastPageAt": now}
    return None, {"state": cur, "since": since, "lastPageAt": last_page}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="journal_silence_watch.py", add_help=True)
    ap.add_argument("--unit", required=True)
    ap.add_argument("--pattern", required=True)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--max-silence-min", type=float, default=30.0)
    ap.add_argument("--still-every-min", type=float, default=180.0)
    ap.add_argument("--lookback-hours", type=float, default=72.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--tz", default="Asia/Seoul")
    ap.add_argument("--now", type=float, default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.max_silence_min <= 0 or args.still_every_min <= 0 or args.lookback_hours <= 0:
        print("journal_silence_watch: durations must be positive", file=sys.stderr)
        return 2
    try:
        pattern = re.compile(args.pattern)
        tz = ZoneInfo(args.tz)
    except (re.error, KeyError, ValueError) as exc:
        print(f"journal_silence_watch: {exc}", file=sys.stderr)
        return 2

    now = args.now if args.now is not None else time.time()
    label = args.label or args.unit
    max_silence = args.max_silence_min * 60
    journalctl = os.environ.get("CCC_JOURNAL_SILENCE_JOURNALCTL") or "journalctl"
    state_path = Path(args.state_file)
    prev = load_state(state_path)

    detail = ""
    last_match: float | None = prev.get("lastMatchAt")
    try:
        found, count = read_last_match(
            journalctl, args.unit, pattern, now - args.lookback_hours * 3600, now
        )
        if found is not None:
            last_match = found
        if found is not None and now - found <= max_silence:
            cur = "ok"
        else:
            cur = "silent"
        window_note = f"최근 {args.lookback_hours:g}시간 일치 {count}건"
    except JournalError as exc:
        cur = "unverified"
        detail = str(exc)
        window_note = "journal 확인 불가"

    kind, nxt = decide(prev, cur, now, args.still_every_min * 60)
    state = {"schema": SCHEMA, "unit": args.unit, "lastMatchAt": last_match,
             "lastRunAt": now, **{k: v for k, v in nxt.items() if k != "outageSince"}}
    try:
        save_state(state_path, state)
    except OSError as exc:
        print(f"UNVERIFIED {label} state-file unwritable: {exc.strerror or exc}")
        return 1

    last_txt = fmt_ts(last_match, tz)
    gap = fmt_span(now - last_match) if last_match is not None else f"{args.lookback_hours:g}시간 이상"
    limit = f"기준 {args.max_silence_min:g}분"
    if kind is None:
        status = {"ok": "OK", "silent": "SILENT", "unverified": "UNVERIFIED"}[cur]
        print(f"{status} {label} 마지막 일치 {last_txt} ({window_note}, {limit}, 알림 없음)")
        return 0
    if kind == "RECOVERED":
        outage = fmt_span(now - float(nxt.get("outageSince", now)))
        print(f"RECOVERED {label} 수신 재개 — 마지막 일치 {last_txt}, 무수신 경보 지속 {outage}")
        return 1
    if cur == "unverified":
        print(f"{kind} UNVERIFIED {label} journal 확인 불가: {detail} "
              f"(마지막으로 본 일치 {last_txt})")
        return 1
    print(f"{kind} {label} 무수신 {gap} — 마지막 일치 {last_txt} ({window_note}, {limit})")
    print(f"점검: 송신 측(기기 자동화 앱) 정체 여부, `systemctl status {args.unit}`")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
