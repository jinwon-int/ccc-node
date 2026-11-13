#!/usr/bin/env python3
"""Restart a phone automation app whose task queue has stalled.

``journal_silence_watch.py`` pages when a webhook receiver goes quiet, but it
runs on the server: it notices 30 minutes late and cannot fix anything. This
is the device-side half. It runs on the phone itself (Termux + local adb) and
reads the automation app's own run log, where a stall has an unmistakable
shape: profile triggers (``P`` lines) keep firing but no task line (``T``/``A``)
follows them. In the outage that motivated it, 995 triggers fired over almost
eleven hours without a single task run, while the app process stayed alive.

A stall is declared when, after the newest task line (or monitor start), there
are at least ``--min-triggers`` trigger lines and the oldest of them is at
least ``--min-age-min`` old. How long the execute service has been alive is
deliberately not used: a healthy service was seen lingering for minutes.

On a stall the watchdog, in this order:

1. saves diagnostics under ``--state-dir/diag/<stamp>/`` (run-log tail, service
   list, service dump, the app's logcat, exit history) so the next stall can be
   explained instead of only cleared;
2. force-stops the app and launches it again;
3. waits for ``--ready-service`` to reappear.

At most one restart happens per ``--cooldown-min``. If the app is stalled again
inside the cooldown, it pages once and leaves the app alone.

Exit codes: ``0`` nothing to page, ``1`` page (stdout is the page), ``2`` usage
error. Run it as an agent-cron ``command`` task with
``--notify telegram-owner-on-failure`` and ``--failure-alert-after 0``, like the
server-side watch: exit 1 is the page.

Only counts and timestamps are printed. Run-log lines and logcat stay in the
diagnostics directory (0700, files 0600) on the device, because an automation
app's log can carry notification text.

Usage:
  tasker_stall_watchdog.py --state-dir DIR
      [--package net.dinglisch.android.taskerm]
      [--runlog /sdcard/Tasker/log/runlog.txt] [--serial 127.0.0.1:5555]
      [--min-triggers 3] [--min-age-min 10] [--cooldown-min 30]
      [--unverified-every-min 180] [--ready-service MonitorService]
      [--ready-timeout-sec 60] [--tail-lines 600] [--keep-diag 5]
      [--label TEXT] [--dry-run] [--now EPOCH]

``CCC_STALL_WATCHDOG_ADB`` overrides the adb binary (tests).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "ccc.tasker-stall-watchdog.v1"
ADB_TIMEOUT_SEC = 30
DIAG_TIMEOUT_SEC = 20
READY_POLL_SEC = 5
LOCK_STALE_SEC = 900
LINE_RE = re.compile(r"^(\d{8}) (\d{2})\.(\d{2})\.(\d{2}) ([A-Z]) +(\S+)")
SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9._/:-]+$")


class AdbError(Exception):
    """adb could not answer; the verdict is UNVERIFIED, never a restart."""


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def adb_shell(adb: str, serial: str, command: str, timeout: float = ADB_TIMEOUT_SEC) -> str:
    """Run one shell command on the device and return its stdout."""
    cmd = [adb, "-s", serial, "shell", command]
    try:
        proc = subprocess.run(
            cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL,
            check=False, timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise AdbError(f"adb failed to start: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        raise AdbError(f"adb exit {proc.returncode}")
    return proc.stdout


def parse_runlog(text: str, now: float) -> list[tuple[float, str, str]]:
    """Return (epoch, kind, status) for each run-log line at or before ``now``.

    Timestamps are device-local without a zone; the watchdog runs on the same
    device, so local time is the right reading. Lines after ``now`` are dropped
    so ``--now`` in the past replays that moment.
    """
    rows: list[tuple[float, str, str]] = []
    for line in text.splitlines():
        match = LINE_RE.match(line)
        if not match:
            continue
        day, hour, minute, second, kind, status = match.groups()
        try:
            stamp = time.mktime(time.strptime(f"{day} {hour}{minute}{second}", "%Y%m%d %H%M%S"))
        except (ValueError, OverflowError):
            continue
        if stamp <= now:
            rows.append((stamp, kind, status))
    return rows


def trailing_triggers(rows: list[tuple[float, str, str]]) -> tuple[int, float | None]:
    """Count trigger lines after the newest task activity.

    Returns (count, epoch of the oldest such trigger). A task or action line
    means the queue is moving; a monitor start means the app was just started.
    Either one clears whatever came before it.
    """
    count = 0
    first: float | None = None
    for stamp, kind, status in rows:
        if kind in ("T", "A") or (kind == "M" and status == "Start"):
            count, first = 0, None
        elif kind == "P":
            count += 1
            if first is None:
                first = stamp
    return count, first


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tsw-", dir=str(path.parent))
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


def acquire_lock(path: Path, now: float) -> bool:
    """Take a one-run lock; a lock older than LOCK_STALE_SEC is replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < LOCK_STALE_SEC:
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        os.utime(str(path), (now, now))
        return True
    return False


def release_lock(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def fmt_span(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def collect_diagnostics(
    adb: str, serial: str, package: str, runlog_text: str, root: Path, now: float, keep: int
) -> Path:
    """Save what can explain the stall. Every step is best-effort."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
    target = root / stamp
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(target, 0o700)
    pkg = shlex.quote(package)
    captures = {
        "services.txt": f"dumpsys activity services {pkg}",
        "service-dump.txt": f"dumpsys activity service {pkg}",
        "logcat.txt": f"logcat -d -v threadtime -t 3000 --pid=$(pidof {pkg})",
        "exit-info.txt": f"dumpsys activity exit-info {pkg}",
    }
    files = {"runlog-tail.txt": runlog_text}
    for name, command in captures.items():
        try:
            files[name] = adb_shell(adb, serial, command, timeout=DIAG_TIMEOUT_SEC)
        except AdbError as exc:
            # A service dump that times out is itself evidence: the app's main
            # thread did not answer.
            files[name] = f"[capture failed: {exc}]\n"
    for name, body in files.items():
        path = target / name
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o600)
    older = sorted(p for p in root.iterdir() if p.is_dir())
    for stale in older[:-keep] if keep > 0 else []:
        shutil.rmtree(stale, ignore_errors=True)
    return target


def restart_app(
    adb: str, serial: str, package: str, ready_service: str, ready_timeout: float
) -> bool:
    """Force-stop and relaunch; True once ``ready_service`` is listed again."""
    pkg = shlex.quote(package)
    adb_shell(adb, serial, f"am force-stop {pkg}")
    adb_shell(adb, serial, f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    waited = 0.0
    while True:
        try:
            listing = adb_shell(adb, serial, f"dumpsys activity services {pkg}")
        except AdbError:
            listing = ""
        if ready_service in listing:
            return True
        if waited >= ready_timeout:
            return False
        _sleep(READY_POLL_SEC)
        waited += READY_POLL_SEC


def run(args: argparse.Namespace, now: float) -> int:
    adb = os.environ.get("CCC_STALL_WATCHDOG_ADB") or "adb"
    label = args.label or "자동화 앱"
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    prev = load_state(state_path)
    last_restart = prev.get("lastRestartAt")
    cooldown = args.cooldown_min * 60
    runlog = shlex.quote(args.runlog)

    def finish(state: str, page: bool, text: str, **extra: Any) -> int:
        record = {
            "schema": SCHEMA, "package": args.package, "state": state, "lastRunAt": now,
            "lastRestartAt": last_restart, "lastPageAt": prev.get("lastPageAt"),
            "since": prev.get("since", now) if prev.get("state") == state else now,
            "restarts": prev.get("restarts", 0),
        }
        record.update(extra)
        if page:
            record["lastPageAt"] = now
        if not args.dry_run:
            try:
                save_state(state_path, record)
            except OSError as exc:
                print(f"UNVERIFIED {label} 상태 파일을 쓸 수 없음: {exc.strerror or exc}")
                return 1
        print(text)
        return 1 if page else 0

    # The rotated file comes first so a stall that spans a rotation is still seen.
    read_cmd = f"cat {runlog}.1 {runlog} 2>/dev/null | tail -n {int(args.tail_lines)}"
    problem = ""
    text = ""
    try:
        text = adb_shell(adb, args.serial, read_cmd)
    except AdbError as exc:
        problem = str(exc)
    rows = parse_runlog(text, now) if not problem else []
    if not problem and not rows:
        problem = "실행 로그가 비었거나 읽을 수 없음"
    if problem:
        prev_page = prev.get("lastPageAt")
        fresh = prev.get("state") != "unverified"
        due = prev_page is None or now - float(prev_page) >= args.unverified_every_min * 60
        if fresh or due:
            kind = "NEW" if fresh else "STILL"
            return finish("unverified", True,
                          f"{kind} UNVERIFIED {label} 정체 여부 확인 불가: {problem}")
        return finish("unverified", False, f"UNVERIFIED {label} 확인 불가: {problem} (알림 없음)")

    count, first = trailing_triggers(rows)
    age = now - first if first is not None else 0.0
    stalled = count >= args.min_triggers and age >= args.min_age_min * 60
    if not stalled:
        note = f"작업 없는 발동 {count}회" if count else "작업 정상"
        if prev.get("state") == "unverified":
            return finish("ok", True, f"RECOVERED {label} 실행 로그 확인 재개 ({note})")
        return finish("ok", False, f"OK {label} {note} (기준 {args.min_triggers}회·"
                                   f"{args.min_age_min:g}분, 알림 없음)")

    summary = f"작업 없이 발동만 {count}회, {fmt_ts(first)}부터 {fmt_span(age)}"
    if args.dry_run:
        print(f"DRY-RUN STALLED {label} {summary} — 실제 실행이면 진단 저장 후 재시작")
        return 0
    if last_restart is not None and now - float(last_restart) < cooldown:
        left = fmt_span(cooldown - (now - float(last_restart)))
        text = (f"STALLED {label} 재시작 뒤에도 정체 — {summary}. "
                f"자동 재시작 보류(남은 대기 {left}), 수동 점검 필요")
        return finish("cooldown", prev.get("state") != "cooldown", text)

    diag = collect_diagnostics(adb, args.serial, args.package, text, state_dir / "diag",
                               now, args.keep_diag)
    last_restart = now
    restarts = int(prev.get("restarts", 0)) + 1
    try:
        ready = restart_app(adb, args.serial, args.package, args.ready_service,
                            args.ready_timeout_sec)
    except AdbError as exc:
        return finish("failed", True,
                      f"RESTART_FAILED {label} {summary} — 재시작 명령 실패: {exc}. 진단 {diag}",
                      restarts=restarts)
    if not ready:
        return finish("failed", True,
                      f"RESTART_FAILED {label} {summary} — 재시작했지만 "
                      f"{args.ready_service} 미확인. 진단 {diag}", restarts=restarts)
    return finish("restarted", True,
                  f"RESTARTED {label} {summary} → 강제 종료 후 재실행, "
                  f"{args.ready_service} 확인. 진단 {diag}", restarts=restarts)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="tasker_stall_watchdog.py", add_help=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--package", default="net.dinglisch.android.taskerm")
    ap.add_argument("--runlog", default="/sdcard/Tasker/log/runlog.txt")
    ap.add_argument("--serial", default="127.0.0.1:5555")
    ap.add_argument("--min-triggers", type=int, default=3)
    ap.add_argument("--min-age-min", type=float, default=10.0)
    ap.add_argument("--cooldown-min", type=float, default=30.0)
    ap.add_argument("--unverified-every-min", type=float, default=180.0)
    ap.add_argument("--ready-service", default="MonitorService")
    ap.add_argument("--ready-timeout-sec", type=float, default=60.0)
    ap.add_argument("--tail-lines", type=int, default=600)
    ap.add_argument("--keep-diag", type=int, default=5)
    ap.add_argument("--label", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", type=float, default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    positive = (args.min_triggers, args.min_age_min, args.cooldown_min,
                args.unverified_every_min, args.ready_timeout_sec, args.tail_lines,
                args.keep_diag)
    if any(value <= 0 for value in positive):
        print("tasker_stall_watchdog: numeric options must be positive", file=sys.stderr)
        return 2
    for name in ("package", "runlog", "serial", "ready_service"):
        if not SAFE_ARG_RE.match(getattr(args, name)):
            print(f"tasker_stall_watchdog: unsafe --{name.replace('_', '-')}", file=sys.stderr)
            return 2

    now = args.now if args.now is not None else time.time()
    if args.dry_run:
        return run(args, now)
    lock = Path(args.state_dir) / "lock"
    if not acquire_lock(lock, time.time()):
        print("SKIP 다른 실행이 진행 중 (알림 없음)")
        return 0
    try:
        return run(args, now)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
