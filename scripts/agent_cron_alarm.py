"""Pure failure classification and consecutive-failure alarm transitions (#1821).

agent-cron recorded every failed run faithfully, yet a node whose claude login
was broken failed prompt tasks for six days with no signal reaching anyone: the
record existed, nothing read it. This module turns a run outcome into

- a bounded ``failureClass`` enum (never raw output), and
- deduplicated alarm transitions for two counters, each bounded so that no
  pattern of run outcomes can produce an alert per run:

  * task counter (``task:<id>``): alerts once when the task reaches N
    consecutive failures; re-alerts within the same streak only when the class
    changes TO ``auth_failed``/``cli_missing`` and at most once per 24h; one
    recovery notice on the next success.
  * node counter (prompt tasks across the node): alerts once per streak when N
    consecutive prompt failures span at least two distinct tasks (one broken
    task is the task counter's job); never re-alerts on a class change; one
    recovery notice, and only when a task that was part of the alerted streak
    succeeds.

No filesystem, environment, or clock access happens here; ``agent_cron.py``
owns the side effects and the state file.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

FAILURE_CLASSES = ('auth_failed', 'cli_missing', 'timeout', 'other')
DEFAULT_FAILURE_ALERT_AFTER = 3
FAILURE_ALERT_AFTER_MAX = 1000
# A class change re-alerts only when it escalates to a class that needs a human
# and retries cannot fix; flapping between exit codes must not.
CLASS_CHANGE_REALERT = ('auth_failed', 'cli_missing')
CLASS_CHANGE_COOLDOWN_SEC = 24 * 3600
NODE_MIN_DISTINCT_TASKS = 2
# Node stale alarm (#1821 proposal 1): no prompt task has succeeded for this
# many days while prompt runs keep failing. Same default as the ccc-doctor
# check; CCC_AGENT_CRON_PROMPT_STALE_DAYS overrides, 0 disables.
DEFAULT_PROMPT_STALE_DAYS = 7
PROMPT_STALE_DAYS_MAX = 366
NODE_TASK_IDS_MAX = 50

# Provider/runner authentication failures, matched on stderr only. The model's
# own result text is never consulted: a task that merely talks about "not
# logged in" must not read as an auth failure.
_AUTH_PATTERN = re.compile(
    r'authentication[_ ](?:failed|error)'
    r'|not logged in'
    r'|please run /login'
    r'|invalid (?:api[ _-]?key|x-api-key)'
    r'|oauth token (?:has )?(?:expired|been revoked)'
    r'|\b401\b[^\n]{0,40}unauthori[sz]ed'
    r'|unauthori[sz]ed[^\n]{0,40}\b401\b',
    re.IGNORECASE,
)
# ccc-headless echoes the head of claude's stdout JSON into stderr after this
# marker on failure. That block carries the model result text, so only an
# unescaped structured error field counts there — inside a JSON string value
# the quotes would be escaped (\") and cannot match.
_HEADLESS_STDOUT_ECHO = re.compile(r'^ccc-headless: stdout (?:\(first|was empty)', re.MULTILINE)
_AUTH_JSON_FIELD = re.compile(
    r'(?<!\\)"(?:error|type)"\s*:\s*"authentication_(?:failed|error)"'
)
_SCAN_CHARS = 16384

TASK_HINTS = {
    'auth_failed': (
        'authentication failed for this task: retries will not help until the '
        'credential is fixed (for a prompt task that is the claude login it runs with)'
    ),
    'cli_missing': (
        'the runner was not found or not executable: for a prompt task check the '
        'claude CLI and the PATH seen by the agent-cron unit, for a command task its argv'
    ),
    'timeout': 'runs are hitting their timeout',
    'other': 'inspect `agent-cron.sh status` and the task runHistory',
}
NODE_HINTS = {
    'auth_failed': (
        'several prompt tasks fail authentication: the node claude login is the '
        'likely cause; re-authenticate it'
    ),
    'cli_missing': (
        'several prompt tasks cannot start the runner: check the claude CLI and '
        'the PATH seen by the agent-cron unit'
    ),
    'timeout': 'several prompt tasks are timing out',
    'other': 'several prompt tasks are failing; inspect `agent-cron.sh status`',
}


def classify_failure(status: Any, exit_code: Any, stderr: Any = '',
                     hint: Any = None) -> str | None:
    """Return the bounded failure class for a run, or None for a success.

    Only the exit code, the runner's spawn hint and stderr are consulted.
    """
    if status == 'success':
        return None
    if status == 'timeout' or exit_code == 124:
        return 'timeout'
    if hint in FAILURE_CLASSES:
        return hint
    if exit_code in (126, 127):
        return 'cli_missing'
    text = str(stderr or '')[:_SCAN_CHARS]
    echo = _HEADLESS_STDOUT_ECHO.search(text)
    own, echoed = (text[:echo.start()], text[echo.start():]) if echo else (text, '')
    if _AUTH_PATTERN.search(own) or _AUTH_JSON_FIELD.search(echoed):
        return 'auth_failed'
    return 'other'


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _epoch(stamp: Any) -> float | None:
    if not isinstance(stamp, str) or not stamp.endswith('Z'):
        return None
    try:
        return datetime.fromisoformat(stamp[:-1] + '+00:00').astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def resolve_threshold(task_value: Any, env_value: Any) -> int:
    """Per-task ``failureAlertAfter`` wins; else the env default; else 3. 0 disables."""
    task_threshold = _nonneg_int(task_value)
    if task_threshold is not None:
        return min(task_threshold, FAILURE_ALERT_AFTER_MAX)
    raw = str(env_value or '').strip()
    if raw.isdigit():
        return min(int(raw), FAILURE_ALERT_AFTER_MAX)
    return DEFAULT_FAILURE_ALERT_AFTER


def resolve_stale_days(env_value: Any) -> int:
    """``CCC_AGENT_CRON_PROMPT_STALE_DAYS``; default 7, 0 disables, capped."""
    raw = str(env_value or '').strip()
    if raw.isdigit():
        return min(int(raw), PROMPT_STALE_DAYS_MAX)
    return DEFAULT_PROMPT_STALE_DAYS


def stale_transition(prev: Any, *, failed: bool, at: str, last_success: str | None,
                     first_run: str | None, days: int
                     ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Node "no prompt success for D days" alarm, once per stale stretch.

    ``prev`` is the node state; only ``staleAlertedAt`` is read or written. A
    prompt success clears it. A failing prompt run alerts when the newest
    prompt success -- or, with none recorded, the oldest recorded prompt run --
    is at least ``days`` old. This catches sparse tasks the consecutive
    counters reach only after weeks; it cannot fire while no prompt run is
    attempted at all (ccc-doctor's check covers a node that simply stopped).
    """
    state = dict(prev) if isinstance(prev, dict) else {}
    if not failed:
        state.pop('staleAlertedAt', None)
        return state, None
    alerted_epoch = _epoch(state.get('staleAlertedAt'))
    success_epoch = _epoch(last_success)
    if alerted_epoch is not None and success_epoch is not None and success_epoch > alerted_epoch:
        # A prompt success this counter did not see (an opted-out task, or a
        # lost state write) still ends the stale stretch.
        state.pop('staleAlertedAt', None)
    if days <= 0 or state.get('staleAlertedAt'):
        return state, None
    basis, since = (('last-success', last_success) if last_success
                    else ('no-success-since-first-run', first_run))
    now_epoch, since_epoch = _epoch(at), _epoch(since)
    if now_epoch is None or since_epoch is None:
        return state, None
    age_days = int((now_epoch - since_epoch) // 86400)
    if age_days < days:
        return state, None
    state['staleAlertedAt'] = at
    return state, {'reason': 'stale', 'days': days, 'ageDays': age_days,
                   'basis': basis, 'since': since}


def trailing_failures(entries: Any) -> int:
    """Count the unbroken run of non-success entries at the end of a history."""
    count = 0
    if not isinstance(entries, list):
        return 0
    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get('status') == 'success':
            break
        count += 1
    return count


def _cooldown_elapsed(last: Any, at: str) -> bool:
    last_epoch, now_epoch = _epoch(last), _epoch(at)
    if last_epoch is None or now_epoch is None:
        return True
    return now_epoch - last_epoch >= CLASS_CHANGE_COOLDOWN_SEC


def task_transition(prev: Any, *, failed: bool, failure_class: str | None,
                    threshold: int, at: str, seed_streak: int = 0
                    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Advance one task counter by one run outcome; returns (state, event).

    The returned state already records any alert as sent: the caller persists
    state BEFORE spooling, so a state-write failure suppresses the alert
    instead of repeating it every run.
    """
    state = dict(prev) if isinstance(prev, dict) else {}
    alerted = state.get('alertedClass') if state.get('alertedClass') in FAILURE_CLASSES else None
    previous_streak = _nonneg_int(state.get('consecutiveFailures')) if prev else None
    if not failed:
        event = None
        if alerted:
            event = {'reason': 'recovered', 'consecutiveFailures': previous_streak or 0,
                     'failureClass': alerted}
        state.update({'consecutiveFailures': 0, 'failureClass': None, 'alertedClass': None,
                      'alertedAt': None, 'classAlertAt': None, 'lastSuccessAt': at})
        return state, event
    if previous_streak is None:
        streak = max(1, _nonneg_int(seed_streak) or 0)
    else:
        streak = previous_streak + 1
    cls = failure_class if failure_class in FAILURE_CLASSES else 'other'
    state.update({'consecutiveFailures': streak, 'failureClass': cls, 'lastFailureAt': at})
    if threshold <= 0 or streak < threshold:
        return state, None
    if alerted is None:
        state.update({'alertedClass': cls, 'alertedAt': at, 'classAlertAt': at})
        return state, {'reason': 'threshold', 'consecutiveFailures': streak, 'failureClass': cls}
    if (cls != alerted and cls in CLASS_CHANGE_REALERT
            and _cooldown_elapsed(state.get('classAlertAt'), at)):
        state.update({'alertedClass': cls, 'classAlertAt': at})
        return state, {'reason': 'class-change', 'consecutiveFailures': streak,
                       'failureClass': cls, 'previousClass': alerted}
    return state, None


def _task_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item not in out:
            out.append(item)
    return out[-NODE_TASK_IDS_MAX:]


_NODE_RESET = {'consecutiveFailures': 0, 'taskIds': [], 'alerted': False,
               'alertedAt': None, 'failureClass': None}


def node_transition(prev: Any, *, task_id: str, failed: bool, failure_class: str | None,
                    threshold: int, at: str, seed_streak: int = 0, seed_ids: Any = (),
                    runnable_ids: Any = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Advance the node-wide prompt counter by one eligible prompt run.

    ``runnable_ids`` (live, enabled prompt tasks that can still run) prunes an
    ALERTED streak's task list on every run; the running task always counts as
    runnable. If none of the alerted tasks can run again (removed, disabled,
    finished one-shots) nothing could ever send its "cleared" notice, so the
    counter resets silently and a new streak can alert. A streak that has not
    alerted yet is not pruned: finished one-shots that each failed once are
    exactly the incident this counter exists to catch.
    """
    state = dict(prev) if isinstance(prev, dict) else {}
    ids = _task_ids(state.get('taskIds'))
    alerted = state.get('alerted') is True
    previous_streak = _nonneg_int(state.get('consecutiveFailures')) if prev else None
    if alerted and runnable_ids is not None:
        keep = set(runnable_ids) | {task_id}
        ids = [item for item in ids if item in keep]
        state['taskIds'] = ids
        if not ids:
            state.update(_NODE_RESET)
            alerted, previous_streak = False, 0
    if not failed:
        state['lastSuccessAt'] = at
        if alerted and task_id not in ids:
            # Another task working says little about the tasks in the alerted
            # streak; keep the alert open until one of THEM recovers.
            return state, None
        event = None
        if alerted:
            event = {'reason': 'recovered', 'consecutiveFailures': previous_streak or 0,
                     'taskIds': ids, 'failureClass': state.get('failureClass')}
        state.update(_NODE_RESET)
        return state, event
    if previous_streak is None:
        streak = max(1, _nonneg_int(seed_streak) or 0)
        ids = _task_ids(list(seed_ids or ()))
    else:
        streak = previous_streak + 1
    if task_id not in ids:
        ids = _task_ids(ids + [task_id])
    cls = failure_class if failure_class in FAILURE_CLASSES else 'other'
    state.update({'consecutiveFailures': streak, 'taskIds': ids, 'failureClass': cls,
                  'lastFailureAt': at, 'alerted': alerted})
    if alerted or threshold <= 0 or streak < threshold or len(ids) < NODE_MIN_DISTINCT_TASKS:
        return state, None
    state.update({'alerted': True, 'alertedAt': at})
    return state, {'reason': 'threshold', 'consecutiveFailures': streak,
                   'failureClass': cls, 'taskIds': ids}


def _id_list(ids: list[str], limit: int = 5) -> str:
    shown = ', '.join(ids[:limit])
    return shown + (f' (+{len(ids) - limit} more)' if len(ids) > limit else '')


def alarm_text(task_id: str, task_event: dict[str, Any] | None,
               node_event: dict[str, Any] | None, node_last_success: str | None,
               stale_event: dict[str, Any] | None = None,
               failure_class: str | None = None) -> str:
    """Owner text built only from task ids, enums, counts and timestamps.

    No stdout/stderr is ever included, so nothing here needs redaction.
    """
    lines = []
    firing = False
    if task_event and task_event['reason'] != 'recovered':
        firing = True
        cls = task_event['failureClass']
        change = (f" (changed from {task_event['previousClass']})"
                  if task_event['reason'] == 'class-change' else '')
        lines.append(f"agent-cron failure alarm: task {task_id} failed "
                     f"{task_event['consecutiveFailures']} consecutive runs, class={cls}{change}")
        lines.append(TASK_HINTS.get(cls, TASK_HINTS['other']))
    if node_event and node_event['reason'] != 'recovered':
        firing = True
        cls = node_event['failureClass']
        ids = node_event['taskIds']
        lines.append(f"agent-cron node alarm: {node_event['consecutiveFailures']} consecutive "
                     f"prompt-task failures across {len(ids)} tasks ({_id_list(ids)}), "
                     f"latest class={cls}; last prompt success: {node_last_success or 'none recorded'}")
        lines.append(NODE_HINTS.get(cls, NODE_HINTS['other']))
    if stale_event:
        firing = True
        since = ('last prompt success: ' + stale_event['since']
                 if stale_event['basis'] == 'last-success'
                 else 'no prompt success recorded since the first prompt run at '
                 + stale_event['since'])
        cls = failure_class if failure_class in FAILURE_CLASSES else 'other'
        lines.append(f"agent-cron stale alarm: no prompt task has succeeded for "
                     f"{stale_event['ageDays']}d (threshold {stale_event['days']}d); "
                     f"{since}; latest failure: task {task_id}, class={cls}")
        if not node_event or node_event['reason'] == 'recovered':
            lines.append(NODE_HINTS.get(cls, NODE_HINTS['other']))
    if task_event and task_event['reason'] == 'recovered':
        lines.append(f"agent-cron failure alarm cleared: task {task_id} succeeded after "
                     f"{task_event['consecutiveFailures']} consecutive failures "
                     f"(was class={task_event['failureClass']})")
    if node_event and node_event['reason'] == 'recovered':
        ids = node_event['taskIds']
        lines.append(f"agent-cron node alarm cleared: prompt task {task_id} succeeded after "
                     f"{node_event['consecutiveFailures']} consecutive prompt-task failures "
                     f"across {len(ids)} tasks ({_id_list(ids)})")
    if firing:
        lines.append('this alarm ignores the task notify setting; opt a task out '
                     'with failureAlertAfter=0')
    if stale_event:
        lines.append('turn the stale alarm off node-wide with CCC_AGENT_CRON_PROMPT_STALE_DAYS=0')
    return '\n'.join(lines)
