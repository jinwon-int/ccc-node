"""Pure failure classification and consecutive-failure alarm transitions (#1821).

agent-cron recorded every failed run faithfully, yet a node whose claude login
was broken failed prompt tasks for six days with no signal reaching anyone: the
record existed, nothing read it. This module turns a run outcome into

- a bounded ``failureClass`` enum (never raw output) for ``runHistory``, and
- a deduplicated alarm transition per key, so the owner hears about a failing
  streak once at the Nth consecutive failure, again only when the failure class
  changes, and once more when the streak recovers.

No filesystem, environment, or clock access happens here; ``agent_cron.py``
owns the side effects.
"""

from __future__ import annotations

import re
from typing import Any

FAILURE_CLASSES = ('auth_failed', 'cli_missing', 'timeout', 'other')
DEFAULT_FAILURE_ALERT_AFTER = 3
FAILURE_ALERT_AFTER_MAX = 1000
NODE_PROMPT_KEY = 'node:prompt'

# Provider authentication failures. ``ccc-headless`` echoes the head of the
# claude JSON body to stderr on failure, which is where the observed
# ``"error":"authentication_failed"`` appears; stdout is scanned too for
# runners that do not. Only the class name leaves this module.
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
# Output handed to the classifier is already capped by agent-cron; this bounds
# the scan again so the classifier stays linear on any caller.
_SCAN_CHARS = 16384

CLASS_HINTS = {
    'auth_failed': (
        'authentication failed: retries will not help until the credential is '
        "fixed; for prompt tasks this is the node's claude login, so every "
        'prompt task on the node keeps failing until it is re-authenticated'
    ),
    'cli_missing': (
        'the runner was not found or not executable: for prompt tasks check the '
        'claude CLI install and the PATH seen by the agent-cron unit, for '
        'command tasks check the argv'
    ),
    'timeout': 'runs are hitting their timeout',
    'other': 'inspect `agent-cron.sh status` and the task runHistory',
}


def classify_failure(status: Any, exit_code: Any, stdout: Any = '', stderr: Any = '',
                     hint: Any = None) -> str | None:
    """Return the bounded failure class for a run, or None for a success."""
    if status == 'success':
        return None
    if status == 'timeout' or exit_code == 124:
        return 'timeout'
    if hint in FAILURE_CLASSES:
        return hint
    if exit_code in (126, 127):
        return 'cli_missing'
    text = f"{str(stderr or '')[:_SCAN_CHARS]}\n{str(stdout or '')[:_SCAN_CHARS]}"
    if _AUTH_PATTERN.search(text):
        return 'auth_failed'
    return 'other'


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def resolve_threshold(task_value: Any, env_value: Any) -> int:
    """Per-task ``failureAlertAfter`` wins; else the env default; else 3. 0 disables."""
    task_threshold = _nonneg_int(task_value)
    if task_threshold is not None:
        return min(task_threshold, FAILURE_ALERT_AFTER_MAX)
    raw = str(env_value or '').strip()
    if raw.isdigit():
        return min(int(raw), FAILURE_ALERT_AFTER_MAX)
    return DEFAULT_FAILURE_ALERT_AFTER


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


def alarm_transition(prev: Any, *, failed: bool, failure_class: str | None,
                     threshold: int, at: str, seed_streak: int = 0
                     ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Advance one alarm key by one run outcome.

    Returns ``(new_state, event)``. ``event`` is None, or a dict whose
    ``reason`` is ``threshold`` (the Nth consecutive failure), ``class-change``
    (still failing, but for a different reason than the owner was told), or
    ``recovered`` (a success after an alert). ``alertedClass`` is NOT set
    here: the caller marks it only after the notification was really spooled,
    so a failed spool write is retried on the next run instead of lost.

    ``seed_streak`` initialises a key that has no state yet (first run after
    upgrade) from the task history, so a node that is already failing alerts
    on its next failure instead of starting the count from zero.
    """
    state = prev if isinstance(prev, dict) else None
    alerted = state.get('alertedClass') if state else None
    alerted = alerted if alerted in FAILURE_CLASSES else None
    previous_streak = _nonneg_int(state.get('consecutiveFailures')) if state else None
    if not failed:
        new_state = {
            'consecutiveFailures': 0,
            'failureClass': None,
            'alertedClass': None,
            'alertedAt': None,
            'lastFailureAt': state.get('lastFailureAt') if state else None,
            'lastSuccessAt': at,
        }
        if alerted:
            return new_state, {
                'reason': 'recovered',
                'consecutiveFailures': previous_streak or 0,
                'failureClass': alerted,
            }
        return new_state, None
    if previous_streak is None:
        streak = max(1, _nonneg_int(seed_streak) or 0)
    else:
        streak = previous_streak + 1
    cls = failure_class if failure_class in FAILURE_CLASSES else 'other'
    new_state = {
        'consecutiveFailures': streak,
        'failureClass': cls,
        'alertedClass': alerted,
        'alertedAt': state.get('alertedAt') if state and alerted else None,
        'lastFailureAt': at,
        'lastSuccessAt': state.get('lastSuccessAt') if state else None,
    }
    event = None
    if threshold > 0 and streak >= threshold:
        if alerted is None:
            event = {'reason': 'threshold', 'consecutiveFailures': streak, 'failureClass': cls}
        elif alerted != cls:
            event = {'reason': 'class-change', 'consecutiveFailures': streak,
                     'failureClass': cls, 'previousClass': alerted}
    return new_state, event


def alarm_text(task_id: str, events: dict[str, dict[str, Any]],
               node_last_success: str | None) -> str:
    """Owner text built only from the task id, enums, counts and timestamps.

    No stdout/stderr is ever included, so nothing here needs redaction.
    """
    lines = []
    task_event = events.get('task')
    node_event = events.get(NODE_PROMPT_KEY)
    recovered = [e for e in (task_event, node_event) if e and e['reason'] == 'recovered']
    firing = [e for e in (task_event, node_event) if e and e['reason'] != 'recovered']
    if firing:
        cls = firing[0]['failureClass']
        lines.append(f'agent-cron failure alarm: class={cls}')
        if task_event and task_event['reason'] != 'recovered':
            lines.append(
                f"task {task_id} failed {task_event['consecutiveFailures']} consecutive runs"
                + (f" (class changed from {task_event['previousClass']})"
                   if task_event['reason'] == 'class-change' else '')
            )
        if node_event and node_event['reason'] != 'recovered':
            lines.append(
                f"node-level: {node_event['consecutiveFailures']} consecutive prompt-task "
                f"failures across tasks; last prompt success: {node_last_success or 'none recorded'}"
                + (f" (class changed from {node_event['previousClass']})"
                   if node_event['reason'] == 'class-change' else '')
            )
        lines.append(CLASS_HINTS.get(cls, CLASS_HINTS['other']))
        lines.append('this alarm ignores the task notify setting; '
                     'opt out per task with failureAlertAfter=0')
    for event in recovered:
        scope = f'task {task_id}' if event is task_event else 'node-level prompt runs'
        lines.append(
            f"agent-cron failure alarm cleared: {scope} succeeded after "
            f"{event['consecutiveFailures']} consecutive failures (was class={event['failureClass']})"
        )
    return '\n'.join(lines)
