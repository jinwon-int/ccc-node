#!/usr/bin/env python3
"""Change-based alerting for the fleet bridge watch (#2086).

``fleet-bridge-watch.sh --state-file PATH`` runs its usual probes, then pipes
the verdict lines through this helper. The helper remembers the last verdict of
every ``<node>/<channel>`` pair and exits nonzero (pages) only when something
changed in a way a person needs to hear about:

* NEW       a pair left OK (or was never seen) and stayed abnormal for N
            consecutive runs (CCC_FLEET_WATCH_CONFIRM, default 2;
            UNVERIFIED uses CCC_FLEET_WATCH_CONFIRM_UNVERIFIED, default 3,
            because a failed inspection is weaker evidence than an answer).
            A pair whose alerted verdict changes to a different abnormal
            verdict is NEW again once the new verdict is confirmed.
* STILL     an alerted pair is still abnormal when its re-alert interval is up
            (CCC_FLEET_WATCH_REALERT, default ``6h,24h``: 6h after the first
            alert, then every 24h; ``off`` disables re-alerts).
* RECOVERED an alerted pair answered OK for CCC_FLEET_WATCH_RECOVER_AFTER
            consecutive runs (default 2). It pages unless
            CCC_FLEET_WATCH_PAGE_RECOVERY=0.

Everything else is context and never pages: PENDING (abnormal, not yet
confirmed), KNOWN (already alerted, re-alert not due), RECOVERING (alerted pair
answering OK, not yet confirmed). OK rows are dropped from the report; a
SUMMARY line keeps the totals. On 2026-10-01 a node lost DNS for ten hours and
the daily watch's alert looked like every other day's, because another node had
been DEGRADED for days and the run failed every day (#2086). Tracking state per
pair is what lets a chronic issue stay quiet without hiding a new one.

The state file is owner-only (0600), replaced atomically under an flock, and
read with symlink and permission checks. An unreadable, unsafe or corrupt file
is reported on stderr and replaced by empty state: losing state costs at most a
repeated alert, never a missed one. Exit codes: 0 no page, 1 page, 2 the state
could not be locked or written (the caller then falls back to raw verdicts).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccc_secure_fs as _secure_fs  # noqa: E402

SCHEMA = 'ccc.fleet-watch-state.v1'
ABNORMAL = ('DOWN', 'UNREACHABLE', 'DRIFT', 'BOOTPATH', 'DUALDOMAIN',
            'NONCANONICAL', 'DEGRADED', 'UNVERIFIED')
VERDICTS = ('OK',) + ABNORMAL
TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
DETAIL_MAX = 200
MAX_STATE_BYTES = 512 * 1024
MAX_ENTRIES = 512
PRUNE_AFTER = timedelta(days=7)
LOCK_TIMEOUT_SEC = 30

NODE_PATTERN = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'
CHANNEL_PATTERN = r'[a-z][a-z0-9_-]{0,15}'
LINE_RE = re.compile(
    r'^(' + '|'.join(VERDICTS) + r')[ \t]+(' + NODE_PATTERN + r')(?:[ \t]+(.*))?$'
)
CHANNEL_RE = re.compile(r'(?:^|[ \t])channel=(' + CHANNEL_PATTERN + r')(?=[ \t]|$)')
KEY_RE = re.compile(r'^' + NODE_PATTERN + '/' + CHANNEL_PATTERN + r'$')
DURATION_RE = re.compile(r'^([1-9][0-9]{0,4})([smhd])$')
DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
DEFAULT_REALERT = '6h,24h'


def warn(message):
    print(f'fleet-watch-state: {message}', file=sys.stderr)


def fmt_ts(value):
    return value.strftime(TS_FORMAT)


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_age(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours >= 48:
        return f'{hours // 24}d{hours % 24:02d}h'
    return f'{hours}h{minutes:02d}m'


def parse_realert(raw):
    """``6h,24h`` -> [6h, 24h]; ``off`` -> []. Invalid input keeps the default."""
    text = (raw or '').strip().lower()
    if text in ('off', 'none', '0'):
        return []
    intervals = []
    for part in text.split(','):
        match = DURATION_RE.match(part.strip())
        if match is None:
            warn(f'invalid CCC_FLEET_WATCH_REALERT; using {DEFAULT_REALERT}')
            return parse_realert(DEFAULT_REALERT)
        intervals.append(timedelta(seconds=int(match.group(1)) * DURATION_UNITS[match.group(2)]))
    return intervals[:8]


def bounded_env_int(name, default, low, high):
    raw = os.environ.get(name, '')
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        warn(f'invalid {name}; using {default}')
        return default
    return min(max(value, low), high)


# --- verdict parsing ---------------------------------------------------------

def parse_verdicts(text):
    """Return ``{key: (verdict, node, channel, detail)}`` in first-seen order.

    Telegram rows carry no channel token; Matrix rows say ``channel=matrix``.
    If a pair is reported twice, an abnormal row wins over an OK one.
    """
    rows = {}
    for raw in (text or '').splitlines():
        match = LINE_RE.match(raw.rstrip())
        if match is None:
            continue
        verdict, node, detail = match.group(1), match.group(2), match.group(3) or ''
        channel_match = CHANNEL_RE.search(detail)
        channel = channel_match.group(1) if channel_match else 'telegram'
        if channel_match:
            detail = detail[:channel_match.start()] + ' ' + detail[channel_match.end():]
        detail = ' '.join(detail.split())[:DETAIL_MAX]
        key = f'{node}/{channel}'
        if key in rows and rows[key][0] != 'OK':
            continue
        rows[key] = (verdict, node, channel, detail)
    return rows


# --- state file --------------------------------------------------------------

def _valid_int(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _valid_optional_ts(entry, field):
    value = entry.get(field)
    return value is None or parse_ts(value) is not None


def valid_entry(key, entry):
    if not isinstance(key, str) or KEY_RE.match(key) is None or not isinstance(entry, dict):
        return False
    if entry.get('verdict') not in VERDICTS or entry.get('alerted') not in ABNORMAL + (None,):
        return False
    if not _valid_int(entry.get('streak'), 1, 10 ** 7):
        return False
    if not _valid_int(entry.get('alertCount', 0), 0, 10 ** 7):
        return False
    detail = entry.get('detail', '')
    if not isinstance(detail, str) or len(detail) > DETAIL_MAX:
        return False
    if parse_ts(entry.get('since')) is None or parse_ts(entry.get('lastSeen')) is None:
        return False
    if entry.get('alerted') is not None and not all(
        parse_ts(entry.get(f)) for f in ('abnormalSince', 'alertedAt', 'lastAlertAt')
    ):
        return False
    return all(_valid_optional_ts(entry, f) for f in ('abnormalSince', 'alertedAt', 'lastAlertAt'))


def _read_state_bytes(path):
    try:
        payload, _ = _secure_fs.read_owner_only_bytes(
            path, max_bytes=MAX_STATE_BYTES, unsafe_mode_mask=0o077
        )
        return payload
    except FileNotFoundError:
        return b''
    except _secure_fs.SecureFsError as error:
        warn(f'state file rejected ({error}); starting from empty state')
    except OSError as error:
        warn(f'state file unreadable ({error.__class__.__name__}); starting from empty state')
    return None


def load_state(path):
    """Return the stored entries; anything unusable is replaced by ``{}``."""
    payload = _read_state_bytes(path)
    if not payload:
        return {}
    try:
        doc = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        warn('state file is not valid JSON; starting from empty state')
        return {}
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA or not isinstance(doc.get('entries'), dict):
        warn('state file has an unknown schema; starting from empty state')
        return {}
    entries = {key: value for key, value in doc['entries'].items() if valid_entry(key, value)}
    dropped = len(doc['entries']) - len(entries)
    if dropped:
        warn(f'dropped {dropped} malformed state entr{"y" if dropped == 1 else "ies"}')
    return entries


def prune(entries, now):
    kept = {
        key: entry for key, entry in entries.items()
        if now - parse_ts(entry['lastSeen']) <= PRUNE_AFTER
    }
    if len(kept) > MAX_ENTRIES:
        newest = sorted(kept, key=lambda k: kept[k]['lastSeen'], reverse=True)[:MAX_ENTRIES]
        kept = {key: kept[key] for key in newest}
    return kept


def save_state(path, entries, now):
    doc = {'schema': SCHEMA, 'updatedAt': fmt_ts(now), 'entries': entries}
    text = json.dumps(doc, ensure_ascii=True, sort_keys=True, indent=1) + '\n'
    _secure_fs.atomic_write_text(path, text, mode=0o600)


# --- transitions -------------------------------------------------------------

class Config:
    def __init__(self):
        self.confirm = bounded_env_int('CCC_FLEET_WATCH_CONFIRM', 2, 1, 50)
        self.confirm_unverified = bounded_env_int('CCC_FLEET_WATCH_CONFIRM_UNVERIFIED', 3, 1, 50)
        self.recover_after = bounded_env_int('CCC_FLEET_WATCH_RECOVER_AFTER', 2, 1, 50)
        self.page_recovery = os.environ.get('CCC_FLEET_WATCH_PAGE_RECOVERY', '1') != '0'
        self.realert = parse_realert(os.environ.get('CCC_FLEET_WATCH_REALERT', DEFAULT_REALERT))

    def confirm_for(self, verdict):
        return self.confirm_unverified if verdict == 'UNVERIFIED' else self.confirm

    def next_alert_at(self, entry):
        if not self.realert or not entry.get('alerted'):
            return None
        index = min(max(entry.get('alertCount', 1), 1) - 1, len(self.realert) - 1)
        return parse_ts(entry['lastAlertAt']) + self.realert[index]


def _open_alert(entry, now):
    entry['alertedAt'] = entry['lastAlertAt'] = fmt_ts(now)
    entry['alertCount'] = 1


def observe_abnormal(prev, verdict, detail, now, cfg):
    """Fold one abnormal row into its entry; return (entry, kind)."""
    stamp = fmt_ts(now)
    same = prev is not None and prev['verdict'] == verdict
    entry = dict(prev or {}, verdict=verdict, detail=detail, lastSeen=stamp)
    entry['streak'] = prev['streak'] + 1 if same else 1
    entry['since'] = prev['since'] if same else stamp
    entry['abnormalSince'] = (prev or {}).get('abnormalSince') or stamp
    entry.setdefault('alerted', None)
    if entry['alerted'] == verdict:
        due = cfg.next_alert_at(entry)
        if due is not None and now >= due:
            entry['lastAlertAt'] = stamp
            entry['alertCount'] = entry.get('alertCount', 0) + 1
            return entry, 'STILL'
        return entry, 'KNOWN'
    if entry['streak'] >= cfg.confirm_for(verdict):
        entry['alerted'] = verdict
        _open_alert(entry, now)
        return entry, 'NEW'
    return entry, 'PENDING'


def _close_alert(entry):
    for field in ('alerted', 'alertedAt', 'lastAlertAt', 'abnormalSince'):
        entry[field] = None
    entry['alertCount'] = 0


def observe_ok(prev, detail, now, cfg):
    """Fold one OK row into its entry; return (entry, kind or None)."""
    stamp = fmt_ts(now)
    same = prev is not None and prev['verdict'] == 'OK'
    entry = dict(prev or {}, verdict='OK', detail=detail, lastSeen=stamp)
    entry['streak'] = prev['streak'] + 1 if same else 1
    entry['since'] = prev['since'] if same else stamp
    if not entry.get('alerted'):
        _close_alert(entry)
        return entry, None
    if entry['streak'] >= cfg.recover_after:
        return entry, 'RECOVERED'
    return entry, 'RECOVERING'


# --- report ------------------------------------------------------------------

def _row_text(kind, verdict, node, channel, detail):
    head = f'{kind} {verdict} {node}' if verdict else f'{kind} {node}'
    return f'{head} channel={channel}' + (f' {detail}' if detail else '')


def describe(kind, row, entry, prev, now, cfg):
    """One report line for a non-OK outcome."""
    verdict, node, channel, detail = row
    if kind in ('RECOVERED', 'RECOVERING'):
        was = prev.get('alerted') or prev.get('verdict')
        line = _row_text(kind, '', node, channel, f'was={was}')
        since = parse_ts(prev.get('abnormalSince'))
        if kind == 'RECOVERED' and since is not None:
            line += f' since={fmt_ts(since)} for={fmt_age(now - since)}'
        if kind == 'RECOVERING':
            line += f' confirm={entry["streak"]}/{cfg.recover_after}'
        return line
    line = _row_text(kind, verdict, node, channel, detail)
    since = parse_ts(entry.get('abnormalSince'))
    if kind == 'PENDING':
        return line + f' confirm={entry["streak"]}/{cfg.confirm_for(verdict)}'
    line += f' since={fmt_ts(since)}'
    if kind == 'STILL':
        line += f' for={fmt_age(now - since)} alerts={entry["alertCount"]}'
    if kind == 'KNOWN':
        due = cfg.next_alert_at(entry)
        line += f' next-alert={fmt_ts(due)}' if due is not None else ' next-alert=off'
    return line


REPORT_ORDER = ('NEW', 'STILL', 'RECOVERED', 'PENDING', 'KNOWN', 'RECOVERING')


def evaluate(entries, rows, now, cfg):
    """Apply one run's rows. Returns (new_entries, {kind: [line, ...]}, ok_count)."""
    updated = dict(entries)
    report = {kind: [] for kind in REPORT_ORDER}
    ok_count = 0
    for key, row in rows.items():
        prev = entries.get(key)
        verdict, _node, _channel, detail = row
        if verdict == 'OK':
            entry, kind = observe_ok(prev, detail, now, cfg)
        else:
            entry, kind = observe_abnormal(prev, verdict, detail, now, cfg)
        if kind is None:
            ok_count += 1
        else:
            report[kind].append(describe(kind, row, entry, prev or {}, now, cfg))
        if kind == 'RECOVERED':
            _close_alert(entry)
        updated[key] = entry
    return prune(updated, now), report, ok_count


def should_page(report, cfg):
    return bool(report['NEW'] or report['STILL'] or (report['RECOVERED'] and cfg.page_recovery))


def render(report, ok_count, checked, page):
    lines = [line for kind in REPORT_ORDER for line in report[kind]]
    counts = ' '.join(f'{kind.lower()}={len(report[kind])}' for kind in REPORT_ORDER)
    lines.append(f'SUMMARY checked={checked} ok={ok_count} {counts} page={"yes" if page else "no"}')
    return '\n'.join(lines)


# --- entry point -------------------------------------------------------------

def parse_now(raw):
    if not raw:
        return datetime.now(timezone.utc).replace(microsecond=0)
    parsed = parse_ts(raw)
    if parsed is None:
        raise SystemExit(f'fleet-watch-state: --now must look like {TS_FORMAT}')
    return parsed


def run(state_path, text, raw_exit, now, cfg):
    rows = parse_verdicts(text)
    if not rows and raw_exit != 0:
        # The watch produced no verdict at all; that is itself a finding.
        rows = parse_verdicts(f'UNVERIFIED watcher inspection=no-verdicts raw-exit={raw_exit}')
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + '.lock')
    with _secure_fs.flock_guard(lock_path, timeout=LOCK_TIMEOUT_SEC) as acquired:
        if not acquired:
            warn('state file is locked by another run')
            return 2
        entries, report, ok_count = evaluate(load_state(state_path), rows, now, cfg)
        save_state(state_path, entries, now)
    page = should_page(report, cfg)
    print(render(report, ok_count, len(rows), page))
    return 1 if page else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--state-file', required=True)
    parser.add_argument('--raw-exit', type=int, default=0)
    parser.add_argument('--now', default=os.environ.get('CCC_FLEET_WATCH_NOW', ''))
    args = parser.parse_args(argv)
    state_path = Path(args.state_file).expanduser()
    if not state_path.is_absolute():
        state_path = Path.cwd() / state_path
    text = sys.stdin.read()
    try:
        return run(state_path, text, args.raw_exit, parse_now(args.now), Config())
    except (OSError, _secure_fs.SecureFsError) as error:
        warn(f'state file could not be updated ({error.__class__.__name__}: {error})')
        return 2


if __name__ == '__main__':
    sys.exit(main())
