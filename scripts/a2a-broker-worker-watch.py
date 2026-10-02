#!/usr/bin/env python3
"""Page on A2A workers a broker has not heard from for too long (#2086).

On 2026-10-01 a node lost external DNS for about 10h. Its A2A worker could not
heartbeat, and the broker marked it ``stale`` in ``GET /workers`` -- but nothing
told a human. This watch reads that broker view on a schedule (agent-cron
command task, every 5 minutes on the broker host) and turns long silences into
fleet signal lines that agent-cron's notifier already counts:

    DOWN <node> source=broker:<name> reason=worker-stale age=37m
    UNREACHABLE broker:<name> reason=timeout age=10m runs=2
    DEGRADED broker:<name> reason=auth-rejected age=10m runs=2
    DEGRADED broker:<name> reason=mass-stale stale=4/5 nodes=a,b,c,d age=20m

Paging rules (state kept in a small owner-only JSON file):

- A node pages only after it has been continuously non-online (stale, or
  missing after having been seen online) for ``--threshold`` (default 15m).
- While it stays down it re-pages on a staged schedule (``--realert``,
  default ``1h,6h``: one hour after the first page, then every six hours).
  Runs in between exit 0, so ``telegram-owner-on-failure`` stays quiet.
- A broker that cannot be queried is its own finding after
  ``--broker-fail-runs`` consecutive failed runs; per-node state is frozen
  meanwhile, so an outage never marks every worker stale.
- After a broker restart (``/livez`` uptime below ``--restart-grace``), a
  recovery from a failed query, or a mass flip (most workers going stale in
  one run), nodes that went non-online inside that window start their clock
  at the end of the window. If most workers are still down past the
  threshold, one broker-level ``mass-stale`` line replaces per-node lines.
- ``--exclude`` (or ``A2A_WORKER_WATCH_EXCLUDE``) skips nodes that are offline
  by design. Identities never seen online by this watch whose last heartbeat
  is older than ``--abandoned-after`` (default 7d) are ignored as leftovers.

Secret handling: the edge secret is sourced by bash from ``--edge-env-file``
(``A2A_EDGE_SECRET``) and fed to curl through ``--config -`` on stdin, so it
never enters this process, any argv, or any output. Raw broker payloads are
never printed -- only node ids, statuses and ages.

Exit codes: 0 nothing to page, 1 page (findings or opted-in recoveries),
2 usage/configuration error (bad flags, unreadable env file, missing secret,
state file not writable).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

EXIT_OK = 0
EXIT_PAGE = 1
EXIT_CONFIG = 2

STATE_VERSION = 1
DEFAULT_EDGE_ENV_FILE = "/root/.a2a-broker-edge.env"
STATE_BASENAME = "a2a-broker-worker-watch.json"
REALERT_SLACK_SEC = 30
MASS_NODE_NAMES_MAX = 20

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DURATION_RE = re.compile(r"^(\d+)([smhd]?)$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

# Exit codes of the fetch shell snippet that mean "operator config", not
# "broker down".
_RC_ENV_UNREADABLE = 90
_RC_SECRET_MISSING = 91
_CURL_REASONS = {
    6: "dns",
    7: "refused",
    28: "timeout",
    35: "tls",
    51: "tls",
    52: "empty-reply",
    56: "conn-reset",
    58: "tls",
    60: "tls",
}

# The secret is sourced from the env file by bash and only ever written to
# curl's stdin config; curl-config escaping (backslash first, then quote)
# keeps hostile bytes literal inside the header value (#1917 pattern).
_WORKERS_SH = r"""
EDGE_ENV=$1; URL=$2; MAXT=$3
. "$EDGE_ENV" >/dev/null 2>&1 || exit 90
S=${A2A_EDGE_SECRET:-}
[ -n "$S" ] || exit 91
S=${S//\\/\\\\}; S=${S//\"/\\\"}
U=${URL//\\/\\\\}; U=${U//\"/\\\"}
printf 'header = "x-a2a-edge-secret: %s"\nurl = "%s/workers"\nmax-time = %s\nconnect-timeout = 5\nwrite-out = "\\n%%{http_code}"\n' "$S" "$U" "$MAXT" | curl -sS --config -
"""

# /livez is public: no secret involved.
_LIVEZ_SH = r"""
URL=$1; MAXT=$2
U=${URL//\\/\\\\}; U=${U//\"/\\\"}
printf 'url = "%s/livez"\nmax-time = %s\nconnect-timeout = 5\nwrite-out = "\\n%%{http_code}"\n' "$U" "$MAXT" | curl -sS --config -
"""


class UsageError(Exception):
    """Bad flags or environment configuration (exit 2)."""


# ------------------------------------------------------------------ settings


@dataclass(frozen=True)
class Broker:
    name: str
    url: str
    env_file: str


@dataclass(frozen=True)
class Settings:
    brokers: tuple[Broker, ...]
    state_file: str
    threshold: int
    realert: tuple[int, ...]
    exclude: frozenset[str]
    report_recovery: bool
    broker_fail_runs: int
    restart_grace: int
    mass_ratio: float
    mass_min: int
    abandoned_after: int
    timeout: int


def parse_duration(raw: str, flag: str) -> int:
    match = _DURATION_RE.match(str(raw).strip())
    if not match:
        raise UsageError(f"{flag}: expected <n>[s|m|h|d], got {raw!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def _csv(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _checked_name(name: str, what: str) -> str:
    if not _NAME_RE.match(name):
        raise UsageError(f"{what}: invalid name {name!r} (allowed: A-Z a-z 0-9 _ . -)")
    return name


def _parse_pairs(items: list[str], flag: str) -> list[tuple[str, str]]:
    pairs = []
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not value.strip():
            raise UsageError(f"{flag}: expected NAME=VALUE, got {item!r}")
        pairs.append((_checked_name(name.strip(), flag), value.strip()))
    return pairs


def _parse_brokers(args: argparse.Namespace, env: dict[str, str]) -> tuple[Broker, ...]:
    raw = list(args.broker or []) or _csv(env.get("A2A_WORKER_WATCH_BROKERS"))
    if not raw:
        raise UsageError("no broker configured: pass --broker NAME=URL (or A2A_WORKER_WATCH_BROKERS)")
    overrides = dict(_parse_pairs(list(args.broker_env_file or []), "--broker-env-file"))
    default_env = args.edge_env_file or env.get("A2A_WORKER_WATCH_EDGE_ENV") or DEFAULT_EDGE_ENV_FILE
    brokers: list[Broker] = []
    for name, url in _parse_pairs(raw, "--broker"):
        if not re.match(r"^https?://[^\s\"\\]+$", url):
            raise UsageError(f"--broker {name}: URL must be http(s):// without spaces or quotes")
        if any(b.name == name for b in brokers):
            raise UsageError(f"--broker {name}: duplicate broker name")
        brokers.append(Broker(name, url.rstrip("/"), overrides.pop(name, default_env)))
    if overrides:
        raise UsageError(f"--broker-env-file: unknown broker(s) {', '.join(sorted(overrides))}")
    return tuple(brokers)


def _default_state_file(env: dict[str, str]) -> str:
    state_dir = env.get("CCC_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "state")
    return os.path.join(state_dir, STATE_BASENAME)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Page on A2A workers a broker has not heard from for too long (#2086).")
    p.add_argument("--broker", action="append", metavar="NAME=URL",
                   help="broker to query (repeatable); default from A2A_WORKER_WATCH_BROKERS")
    p.add_argument("--edge-env-file", metavar="PATH",
                   help="env file exporting A2A_EDGE_SECRET, sourced by bash only "
                        f"(default A2A_WORKER_WATCH_EDGE_ENV or {DEFAULT_EDGE_ENV_FILE})")
    p.add_argument("--broker-env-file", action="append", metavar="NAME=PATH",
                   help="per-broker env file override (repeatable)")
    p.add_argument("--state-file", metavar="PATH",
                   help=f"default A2A_WORKER_WATCH_STATE or $CCC_STATE_DIR/{STATE_BASENAME}")
    p.add_argument("--threshold", default="15m", help="continuous non-online time before paging")
    p.add_argument("--realert", default="1h,6h",
                   help="staged re-page intervals while still down; the last one repeats")
    p.add_argument("--exclude", action="append", metavar="NODES",
                   help="comma-separated nodes never paged (merged with A2A_WORKER_WATCH_EXCLUDE)")
    p.add_argument("--report-recovery", action="store_true",
                   help="page (exit 1) once when a paged node or broker recovers")
    p.add_argument("--broker-fail-runs", type=int, default=2,
                   help="consecutive failed broker queries before paging")
    p.add_argument("--restart-grace", default="10m",
                   help="grace window after a broker restart / recovery / mass flip")
    p.add_argument("--mass-ratio", type=float, default=0.8,
                   help="fraction of workers that counts as a mass flip / mass-stale")
    p.add_argument("--mass-min", type=int, default=3, help="minimum node count for mass handling")
    p.add_argument("--abandoned-after", default="7d",
                   help="ignore never-seen-online identities silent for longer than this")
    p.add_argument("--timeout", default="15s", help="per-request curl max time")
    return p


def parse_settings(argv: list[str] | None, env: dict[str, str]) -> Settings:
    args = build_parser().parse_args(argv)
    realert = tuple(parse_duration(x, "--realert") for x in _csv(args.realert))
    if not realert or min(realert) <= 0:
        raise UsageError("--realert needs at least one positive interval")
    if args.broker_fail_runs < 1 or args.mass_min < 2 or not 0 < args.mass_ratio <= 1:
        raise UsageError("--broker-fail-runs >= 1, --mass-min >= 2, 0 < --mass-ratio <= 1")
    exclude = set(_csv(env.get("A2A_WORKER_WATCH_EXCLUDE")))
    for item in args.exclude or []:
        exclude.update(_csv(item))
    for node in exclude:
        _checked_name(node, "--exclude")
    timeout = parse_duration(args.timeout, "--timeout")
    if not 1 <= timeout <= 120:
        raise UsageError("--timeout must be between 1s and 120s")
    return Settings(
        brokers=_parse_brokers(args, env),
        state_file=args.state_file or env.get("A2A_WORKER_WATCH_STATE") or _default_state_file(env),
        threshold=parse_duration(args.threshold, "--threshold"),
        realert=realert,
        exclude=frozenset(exclude),
        report_recovery=bool(args.report_recovery),
        broker_fail_runs=args.broker_fail_runs,
        restart_grace=parse_duration(args.restart_grace, "--restart-grace"),
        mass_ratio=args.mass_ratio,
        mass_min=args.mass_min,
        abandoned_after=parse_duration(args.abandoned_after, "--abandoned-after"),
        timeout=timeout,
    )


# --------------------------------------------------------------------- fetch


@dataclass
class Worker:
    node: str
    online: bool
    status: str
    last_seen: float | None = None


@dataclass
class Fetch:
    ok: bool
    workers: list[Worker] = field(default_factory=list)
    kind: str = ""  # failure kind: "unreachable" | "degraded" | "config"
    reason: str = ""
    uptime: float | None = None
    draining: bool = False


def safe_node_id(raw: Any) -> str | None:
    """Broker-supplied ids are untrusted: anything outside the name charset is
    replaced by a stable hash so it can never forge an output line."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    node = raw.strip()
    if _NAME_RE.match(node):
        return node
    return "node-" + hashlib.sha256(node.encode("utf-8", "replace")).hexdigest()[:8]


def _parse_ts(raw: Any) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_workers(payload: Any) -> list[Worker] | None:
    """``{"items": [...]}`` -> workers merged per node (online if any row is)."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    merged: dict[str, Worker] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        node = safe_node_id(row.get("nodeId"))
        if node is None:
            continue
        raw_status = row.get("status")
        status = raw_status if isinstance(raw_status, str) and _NAME_RE.match(raw_status) else "unknown"
        worker = Worker(node, status == "online", status, _parse_ts(row.get("lastSeenAt")))
        prev = merged.get(node)
        if prev is None or (worker.online and not prev.online):
            merged[node] = worker
        elif prev.last_seen is None or (worker.last_seen or 0) > prev.last_seen:
            prev.last_seen = worker.last_seen
    return [merged[k] for k in sorted(merged)]


def _split_http(stdout: bytes) -> tuple[int, bytes]:
    body, _, code = stdout.rpartition(b"\n")
    try:
        return int(code.strip() or b"0"), body
    except ValueError:
        return 0, body


def _run(script: str, args: list[str], timeout: int) -> tuple[int, bytes]:
    proc = subprocess.run(["bash", "-c", script, "_", *args],
                          capture_output=True, timeout=timeout + 10, check=False)
    return proc.returncode, proc.stdout


def _http_failure(code: int) -> Fetch:
    if code in (401, 403):
        return Fetch(False, kind="degraded", reason="auth-rejected")
    if code >= 500 or code == 0:
        return Fetch(False, kind="unreachable", reason=f"http-{code}")
    return Fetch(False, kind="degraded", reason=f"http-{code}")


def _fetch_workers(broker: Broker, timeout: int) -> Fetch:
    try:
        rc, stdout = _run(_WORKERS_SH, [broker.env_file, broker.url, str(timeout)], timeout)
    except subprocess.TimeoutExpired:
        return Fetch(False, kind="unreachable", reason="timeout")
    except OSError:
        return Fetch(False, kind="config", reason="bash-unavailable")
    if rc == _RC_ENV_UNREADABLE:
        return Fetch(False, kind="config", reason="env-unreadable")
    if rc == _RC_SECRET_MISSING:
        return Fetch(False, kind="config", reason="secret-missing")
    if rc == 127:
        return Fetch(False, kind="config", reason="curl-missing")
    if rc != 0:
        return Fetch(False, kind="unreachable", reason=_CURL_REASONS.get(rc, f"curl-exit-{rc}"))
    code, body = _split_http(stdout)
    if code != 200:
        return _http_failure(code)
    try:
        workers = parse_workers(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        workers = None
    if workers is None:
        return Fetch(False, kind="degraded", reason="bad-payload")
    return Fetch(True, workers=workers)


def _fetch_livez(broker: Broker, timeout: int) -> tuple[float | None, bool]:
    """Best effort (uptimeSec, draining); (None, False) when unavailable."""
    try:
        rc, stdout = _run(_LIVEZ_SH, [broker.url, str(timeout)], timeout)
        code, body = _split_http(stdout)
        payload = json.loads(body.decode("utf-8")) if rc == 0 and code == 200 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError):
        return None, False
    if not isinstance(payload, dict):
        return None, False
    uptime = payload.get("uptimeSec")
    uptime_val = float(uptime) if isinstance(uptime, (int, float)) and uptime >= 0 else None
    return uptime_val, payload.get("draining") is True


def fetch_broker(broker: Broker, timeout: int) -> Fetch:
    result = _fetch_workers(broker, timeout)
    if result.ok:
        result.uptime, result.draining = _fetch_livez(broker, timeout)
    return result


# --------------------------------------------------------------------- state


def _fresh_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "brokers": {}}


def load_state(path: str, warn: Callable[[str], None]) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _fresh_state()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        warn("WARN state-reset reason=corrupt")
        return _fresh_state()
    if (not isinstance(data, dict) or data.get("version") != STATE_VERSION
            or not isinstance(data.get("brokers"), dict)):
        warn("WARN state-reset reason=bad-shape")
        return _fresh_state()
    return data


def save_state(path: str, state: dict[str, Any]) -> None:
    """Atomic owner-only write: temp file in the same dir, fsync, rename."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".a2a-worker-watch.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _num(holder: dict[str, Any], key: str) -> float | None:
    value = holder.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dict(holder: dict[str, Any], key: str) -> dict[str, Any]:
    value = holder.get(key)
    if not isinstance(value, dict):
        value = {}
        holder[key] = value
    return value


# ---------------------------------------------------------------- evaluation


def fmt_age(seconds: float) -> str:
    sec = max(0, int(seconds))
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    return f"{sec // 86400}d{(sec % 86400) // 3600}h"


@dataclass
class Report:
    page: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    config_error: bool = False
    paged: bool = False

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


def _alert_due(cfg: Settings, holder: dict[str, Any], now: float) -> bool:
    count = int(_num(holder, "alerts") or 0)
    last = _num(holder, "lastAlertAt")
    if count <= 0 or last is None:
        return True
    interval = cfg.realert[min(count - 1, len(cfg.realert) - 1)]
    return now - last >= interval - REALERT_SLACK_SEC


def _next_alert_in(cfg: Settings, holder: dict[str, Any], now: float) -> str:
    count = int(_num(holder, "alerts") or 0)
    interval = cfg.realert[min(max(count, 1) - 1, len(cfg.realert) - 1)]
    return fmt_age(interval - (now - (_num(holder, "lastAlertAt") or now)))


def _mark_alerted(holder: dict[str, Any], now: float) -> None:
    holder["alerts"] = int(_num(holder, "alerts") or 0) + 1
    holder["lastAlertAt"] = now


def _page_or_hold(cfg: Settings, rep: Report, holder: dict[str, Any], now: float,
                  line: str, ongoing: str) -> None:
    if _alert_due(cfg, holder, now):
        rep.page.append(line)
        rep.paged = True
        _mark_alerted(holder, now)
        rep.bump("paged")
    else:
        rep.info.append(f"ONGOING {ongoing} next_alert_in={_next_alert_in(cfg, holder, now)}")
        rep.bump("ongoing")


def _broker_failed(cfg: Settings, name: str, fetch: Fetch, bs: dict[str, Any],
                   now: float, rep: Report) -> None:
    runs = int(_num(bs, "failRuns") or 0) + 1
    bs["failRuns"] = runs
    since = _num(bs, "failSince")
    if since is None:
        since = now
        bs["failSince"] = now
    label = f"broker:{name} reason={fetch.reason} age={fmt_age(now - since)} runs={runs}"
    if runs < cfg.broker_fail_runs:
        rep.info.append(f"PENDING {label}")
        rep.bump("pending")
        return
    token = "UNREACHABLE" if fetch.kind == "unreachable" else "DEGRADED"
    _page_or_hold(cfg, rep, _dict(bs, "failAlert"), now, f"{token} {label}", label)


def _merge_grace(bs: dict[str, Any], start: float, until: float) -> None:
    grace = bs.get("grace")
    if isinstance(grace, dict) and _num(grace, "start") is not None and _num(grace, "until") is not None:
        start = min(start, _num(grace, "start") or start)
        until = max(until, _num(grace, "until") or until)
    bs["grace"] = {"start": start, "until": until}


def _broker_recovered(cfg: Settings, name: str, bs: dict[str, Any], now: float, rep: Report) -> None:
    since = _num(bs, "failSince")
    alert = bs.pop("failAlert", None)
    bs.pop("failRuns", None)
    bs.pop("failSince", None)
    if since is None:
        return
    # Workers re-heartbeat right after the broker comes back: give them time.
    _merge_grace(bs, since, now + cfg.restart_grace)
    if isinstance(alert, dict) and (_num(alert, "alerts") or 0) > 0:
        rep.info.append(f"RECOVERED broker:{name} down={fmt_age(now - since)}")
        rep.paged = rep.paged or cfg.report_recovery


def _restart_grace(cfg: Settings, fetch: Fetch, bs: dict[str, Any], now: float) -> None:
    if fetch.draining:
        _merge_grace(bs, now, now + cfg.restart_grace)
    elif fetch.uptime is not None and fetch.uptime < cfg.restart_grace:
        started = now - fetch.uptime
        # A drain precedes the restart; one minute of slack covers it.
        _merge_grace(bs, started - 60, started + cfg.restart_grace)


def _mass_flip(cfg: Settings, nodes: dict[str, Any], current: dict[str, Worker],
               bs: dict[str, Any], now: float) -> None:
    """Most previously-online workers going non-online in one run looks like a
    broker-side event (restart, network): open a grace window for them."""
    was_online = [n for n, ns in nodes.items()
                  if n not in cfg.exclude and isinstance(ns, dict) and ns.get("seenOnline")
                  and _num(ns, "since") is None and n in current]
    flipped = [n for n in was_online if not current[n].online]
    if len(flipped) >= cfg.mass_min and len(flipped) >= cfg.mass_ratio * len(was_online):
        _merge_grace(bs, now, now + cfg.restart_grace)


@dataclass
class Finding:
    node: str
    reason: str
    age: float


def _apply_grace(ns: dict[str, Any], grace: Any, since: float) -> float:
    clock = _num(ns, "clock")
    clock = since if clock is None else clock
    if isinstance(grace, dict):
        start, until = _num(grace, "start"), _num(grace, "until")
        if start is not None and until is not None and start <= since < until:
            clock = max(clock, until)
    ns["clock"] = clock
    return clock


def _node_recovered(cfg: Settings, name: str, node: str, ns: dict[str, Any],
                    now: float, rep: Report) -> None:
    since = _num(ns, "since")
    if since is not None and (_num(ns, "alerts") or 0) > 0:
        rep.info.append(f"RECOVERED {node} source=broker:{name} down={fmt_age(now - since)}")
        rep.paged = rep.paged or cfg.report_recovery


def _update_node(cfg: Settings, name: str, w: Worker, nodes: dict[str, Any],
                 bs: dict[str, Any], now: float, rep: Report) -> Finding | None:
    prev = nodes.get(w.node)
    ns: dict[str, Any] = prev if isinstance(prev, dict) else {}
    if w.online:
        _node_recovered(cfg, name, w.node, ns, now, rep)
        nodes[w.node] = {"seenOnline": True}
        rep.bump("online")
        return None
    if w.node in cfg.exclude:
        nodes[w.node] = {"seenOnline": bool(ns.get("seenOnline"))}
        rep.bump("excluded")
        return None
    if not ns.get("seenOnline") and w.last_seen is not None and now - w.last_seen > cfg.abandoned_after:
        nodes[w.node] = {}
        rep.bump("ignored")
        return None
    since = _num(ns, "since")
    since = now if since is None else since
    ns = {**ns, "since": since}
    nodes[w.node] = ns
    clock = _apply_grace(ns, bs.get("grace"), since)
    reason = "worker-missing" if w.status == "missing" else f"worker-{w.status}"
    observed = since if w.last_seen is None else min(since, w.last_seen)
    if now - clock < cfg.threshold:
        rep.info.append(f"PENDING {w.node} source=broker:{name} reason={reason} "
                        f"age={fmt_age(now - observed)}")
        rep.bump("pending")
        return None
    return Finding(w.node, reason, now - observed)


def _emit_findings(cfg: Settings, name: str, findings: list[Finding], tracked: int,
                   nodes: dict[str, Any], bs: dict[str, Any], now: float, rep: Report) -> None:
    if len(findings) >= cfg.mass_min and len(findings) >= cfg.mass_ratio * tracked:
        names = ",".join(f.node for f in findings[:MASS_NODE_NAMES_MAX])
        more = "" if len(findings) <= MASS_NODE_NAMES_MAX else f",+{len(findings) - MASS_NODE_NAMES_MAX}"
        label = (f"broker:{name} reason=mass-stale stale={len(findings)}/{tracked} "
                 f"nodes={names}{more} age={fmt_age(max(f.age for f in findings))}")
        holder = _dict(bs, "massAlert")
        due = _alert_due(cfg, holder, now)
        _page_or_hold(cfg, rep, holder, now, f"DEGRADED {label}", label)
        for f in findings:  # recorded as paged so a partial heal does not re-page at once
            if due:
                _mark_alerted(nodes[f.node], now)
        return
    bs.pop("massAlert", None)
    for f in findings:
        label = f"{f.node} source=broker:{name} reason={f.reason} age={fmt_age(f.age)}"
        _page_or_hold(cfg, rep, nodes[f.node], now, f"DOWN {label}", label)


def _broker_ok(cfg: Settings, name: str, fetch: Fetch, bs: dict[str, Any],
               now: float, rep: Report) -> None:
    _broker_recovered(cfg, name, bs, now, rep)
    _restart_grace(cfg, fetch, bs, now)
    nodes = _dict(bs, "nodes")
    current = {w.node: w for w in fetch.workers}
    for node, ns in list(nodes.items()):
        if (node not in current and node not in cfg.exclude
                and isinstance(ns, dict) and ns.get("seenOnline")):
            current[node] = Worker(node, False, "missing")
    _mass_flip(cfg, nodes, current, bs, now)
    findings: list[Finding] = []
    for node in sorted(current):
        finding = _update_node(cfg, name, current[node], nodes, bs, now, rep)
        if finding is not None:
            findings.append(finding)
    for node in [n for n in nodes if n not in current]:
        del nodes[node]
    rep.bump("workers", len(current))
    # Tracked = neither excluded nor ignored (an ignored identity keeps {}).
    tracked = sum(1 for n in current if n not in cfg.exclude and nodes.get(n))
    _emit_findings(cfg, name, findings, max(tracked, len(findings)), nodes, bs, now, rep)
    grace = bs.get("grace")
    if isinstance(grace, dict) and now >= (_num(grace, "until") or 0):
        bs.pop("grace", None)


def evaluate(cfg: Settings, state: dict[str, Any], fetches: dict[str, Fetch], now: float) -> Report:
    rep = Report()
    brokers = state["brokers"]
    for broker in cfg.brokers:
        fetch = fetches[broker.name]
        if fetch.kind == "config":
            rep.page.append(f"UNVERIFIED broker:{broker.name} reason={fetch.reason}")
            rep.config_error = True
            continue
        bs = _dict(brokers, broker.name)
        if fetch.ok:
            rep.bump("brokersOk")
            _broker_ok(cfg, broker.name, fetch, bs, now, rep)
        else:
            _broker_failed(cfg, broker.name, fetch, bs, now, rep)
    for name in [n for n in brokers if n not in {b.name for b in cfg.brokers}]:
        del brokers[name]
    return rep


def render(cfg: Settings, rep: Report) -> list[str]:
    c = rep.counts
    summary = (f"SUMMARY brokers={c.get('brokersOk', 0)}/{len(cfg.brokers)} "
               f"workers={c.get('workers', 0)} online={c.get('online', 0)} "
               f"paged={c.get('paged', 0)} ongoing={c.get('ongoing', 0)} "
               f"pending={c.get('pending', 0)} excluded={c.get('excluded', 0)} "
               f"ignored={c.get('ignored', 0)} threshold={fmt_age(cfg.threshold)}")
    return [*rep.page, *rep.info, summary]


# ---------------------------------------------------------------------- main


def run(cfg: Settings, fetcher: Callable[[Broker, int], Fetch], now: float,
        out: Callable[[str], None], warn: Callable[[str], None]) -> int:
    lock_path = cfg.state_file + ".lock"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.state_file)), mode=0o700, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        warn("UNVERIFIED watch reason=state-unwritable")
        return EXIT_CONFIG
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            out("SKIP another run holds the state lock")
            return EXIT_OK
        state = load_state(cfg.state_file, warn)
        fetches = {b.name: fetcher(b, cfg.timeout) for b in cfg.brokers}
        rep = evaluate(cfg, state, fetches, now)
        try:
            save_state(cfg.state_file, state)
        except OSError:
            warn("UNVERIFIED watch reason=state-unwritable")
            rep.config_error = True
        for line in render(cfg, rep):
            out(line)
    finally:
        os.close(lock_fd)
    if rep.config_error:
        return EXIT_CONFIG
    return EXIT_PAGE if rep.paged else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = parse_settings(argv, dict(os.environ))
    except UsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return run(cfg, fetch_broker, float(int(time.time())),
               lambda line: print(line, flush=True),
               lambda line: print(line, file=sys.stderr, flush=True))


if __name__ == "__main__":
    sys.exit(main())
