#!/usr/bin/env python3
"""Shared-device screen lease (#2927) — one node at a time changes a shared phone's screen.

The lease lives on the device itself (default ``~/.ccc-node/device-lease.json``),
so every controller sees the same record whether it reaches the phone over ssh
or over adb TCP (an adb TCP connection leaves no ssh trace, so the lease must
name the node). Read-only work (``dumpsys``, ``logcat``, ``screencap``) needs no
lease; anything that changes the screen (tap, swipe, key, ``am start``, CDP
click) takes one first and releases it when done.

    device_lease.py --target phone-a status
    device_lease.py --target phone-a acquire --purpose "Play 0.1.7 출시" [--ttl 15m] [--wait 300]
    device_lease.py --target phone-a renew   [--ttl 15m]
    device_lease.py --target phone-a release

``--target HOST`` runs this same file on the device (``ssh HOST python3 - …``);
``--local`` runs it here (on the device, or in tests with ``--state-file``).
Holder identity is ``--node`` (default ``$CCC_NODE`` or the short hostname) plus
``--session`` (default ``$CCC_LEASE_SESSION`` or ``<node>-<pid of the caller>``).
An expired lease counts as free. Nobody can take or drop another node's live
lease from this tool — that is an owner decision.

Exit codes: 0 ok (acquired / renewed / released / status printed) · 2 usage ·
3 busy (held by someone else; holder printed) · 4 not the holder · 5 remote or
state error.
"""
import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

DEFAULT_STATE = "~/.ccc-node/device-lease.json"
DEFAULT_TTL = 15 * 60
MAX_TTL = 2 * 60 * 60
EXIT_OK, EXIT_USAGE, EXIT_BUSY, EXIT_NOT_HOLDER, EXIT_ERROR = 0, 2, 3, 4, 5


def parse_ttl(text):
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", text or "")
    if not m:
        raise ValueError(f"bad --ttl {text!r} (use 900, 900s, 15m, 1h)")
    secs = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if not 60 <= secs <= MAX_TTL:
        raise ValueError(f"--ttl must be between 60s and {MAX_TTL // 60}m")
    return secs


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A torn or hand-edited file must not read as "free": fail closed.
        raise


def _write(path, rec):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def live(rec, now):
    return bool(rec) and int(rec.get("expires_at_unix", 0)) > now


def apply(op, state_path, *, node, session, purpose="", ttl=DEFAULT_TTL, now=None):
    """One locked read-modify-write. Returns (exit_code, result dict)."""
    import fcntl  # POSIX only; imported here so --target works from anywhere

    now = int(time.time() if now is None else now)
    state_path = os.path.expanduser(state_path)
    os.makedirs(os.path.dirname(state_path) or ".", mode=0o700, exist_ok=True)
    with open(state_path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rec = _read(state_path)
        except (OSError, ValueError) as exc:
            return EXIT_ERROR, {"ok": False, "error": f"unreadable lease file: {exc}"}
        held = live(rec, now)
        mine = held and rec.get("node") == node and rec.get("session") == session
        base = {"op": op, "now": _iso(now), "state_file": state_path}
        if op == "status":
            return EXIT_OK, {**base, "ok": True, "held": held, "lease": rec if held else None,
                             "expired": rec if (rec and not held) else None}
        if op == "acquire":
            if held and not mine:
                return EXIT_BUSY, {**base, "ok": False, "busy": True, "lease": rec}
            new = {"node": node, "session": session, "purpose": purpose,
                   "acquired_at": rec["acquired_at"] if mine else _iso(now),
                   "expires_at": _iso(now + ttl), "expires_at_unix": now + ttl}
            _write(state_path, new)
            return EXIT_OK, {**base, "ok": True, "lease": new, "renewed": bool(mine)}
        if op == "renew":
            if not mine:
                return EXIT_NOT_HOLDER, {**base, "ok": False, "lease": rec if held else None}
            rec.update(expires_at=_iso(now + ttl), expires_at_unix=now + ttl)
            _write(state_path, rec)
            return EXIT_OK, {**base, "ok": True, "lease": rec}
        if op == "release":
            if not held:
                return EXIT_OK, {**base, "ok": True, "released": False, "note": "no live lease"}
            if not mine:
                return EXIT_NOT_HOLDER, {**base, "ok": False, "lease": rec}
            os.unlink(state_path)
            return EXIT_OK, {**base, "ok": True, "released": True}
    return EXIT_USAGE, {"ok": False, "error": f"unknown op {op}"}


def _default_node():
    return os.environ.get("CCC_NODE", "").strip() or socket.gethostname().split(".")[0]


def build_parser():
    p = argparse.ArgumentParser(description="Shared-device screen lease (#2927)")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--target", help="ssh host of the device (runs this file there)")
    where.add_argument("--local", action="store_true", help="operate on the lease file on this host")
    p.add_argument("--state-file", default=DEFAULT_STATE)
    p.add_argument("--node", default=None)
    p.add_argument("--session", default=None)
    p.add_argument("--remote-python", default="python3")
    p.add_argument("--ssh-timeout", type=int, default=20)
    p.add_argument("op", choices=["status", "acquire", "renew", "release"])
    p.add_argument("--purpose", default="")
    p.add_argument("--ttl", default="15m")
    p.add_argument("--wait", type=int, default=0, help="acquire: poll up to N seconds while busy")
    return p


def run_remote(args, argv_rest):
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()
    # ssh hands the remote shell one string: quote every word, or a --purpose
    # with spaces (or a shell metacharacter) would be re-split there.
    remote = " ".join(shlex.quote(a) for a in [args.remote_python, "-", "--local"] + argv_rest)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={args.ssh_timeout}", args.target, remote]
    try:
        proc = subprocess.run(cmd, input=src, text=True, capture_output=True,
                              timeout=args.ssh_timeout + 30 + max(args.wait, 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": f"ssh {args.target}: {exc}"}, ensure_ascii=False))
        return EXIT_ERROR
    sys.stdout.write(proc.stdout)
    if proc.returncode not in (EXIT_OK, EXIT_BUSY, EXIT_NOT_HOLDER, EXIT_USAGE):
        sys.stderr.write(proc.stderr[-2000:])
        return EXIT_ERROR
    return proc.returncode


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        ttl = parse_ttl(args.ttl)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return EXIT_USAGE
    if args.op == "acquire" and not args.purpose.strip():
        print(json.dumps({"ok": False, "error": "acquire needs --purpose"}, ensure_ascii=False))
        return EXIT_USAGE
    node = args.node or _default_node()
    # The session is fixed on the controller, never on the device: a remote run
    # would otherwise invent a new pid-based session per call and never match.
    session = args.session or os.environ.get("CCC_LEASE_SESSION", "").strip() or f"{node}-{os.getppid()}"
    if args.target:
        rest = ["--state-file", args.state_file, "--node", node, "--session", session,
                "--purpose", args.purpose, "--ttl", args.ttl, "--wait", str(args.wait), args.op]
        return run_remote(args, rest)
    deadline = time.time() + max(args.wait, 0)
    while True:
        rc, out = apply(args.op, args.state_file, node=node, session=session,
                        purpose=args.purpose.strip()[:200], ttl=ttl)
        if rc != EXIT_BUSY or time.time() >= deadline:
            break
        time.sleep(min(15, max(1, deadline - time.time())))
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
