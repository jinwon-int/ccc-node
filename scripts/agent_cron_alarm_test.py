#!/usr/bin/env python3
"""Tests for agent-cron failure classification and the bounded alarm (#1821).

The incident: a node's claude login broke and prompt tasks failed for six days
(`exited 1` + `"error":"authentication_failed"`) with no alarm. Unit tests pin
the transitions; the integration scenarios drive the real CLI through a fake
runner and COUNT alarm spool files over a run sequence, asserting that no
outcome pattern can produce an alarm per run.

Every integration run uses a from-scratch environment with a temporary HOME,
store and push spool, so no run can reach a node's live spool or state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agent_cron_alarm import (  # noqa: E402
    FAILURE_CLASSES,
    alarm_text,
    classify_failure,
    node_transition,
    resolve_stale_days,
    resolve_threshold,
    stale_transition,
    task_transition,
    trailing_failures,
)
import agent_cron_schema  # noqa: E402

AUTH_STDERR = (
    'ccc-headless: /usr/bin/claude exited 1\n'
    'ccc-headless: stdout (first 2000B of 70B):\n'
    '{"type":"result","is_error":true,"error":"authentication_failed"}\n'
)


class ClassifyFailure(unittest.TestCase):
    def test_success_has_no_class(self) -> None:
        self.assertIsNone(classify_failure('success', 0, AUTH_STDERR))

    def test_observed_auth_failure(self) -> None:
        self.assertEqual(classify_failure('failed', 1, AUTH_STDERR), 'auth_failed')

    def test_auth_variants_on_stderr(self) -> None:
        for text in ('Not logged in · Please run /login', 'Invalid API key',
                     'OAuth token has expired', 'HTTP 401 Unauthorized'):
            with self.subTest(text=text):
                self.assertEqual(classify_failure('failed', 1, text), 'auth_failed')

    def test_model_result_text_is_not_auth(self) -> None:
        # The echoed stdout carries the model's own words; only an unescaped
        # structured error field counts there.
        echoed = ('ccc-headless: claude exited 1\n'
                  'ccc-headless: stdout (first 2000B of 99B):\n'
                  '{"type":"result","result":"the page said \\"Not logged in\\" '
                  'and \\"error\\":\\"authentication_failed\\""}\n')
        self.assertEqual(classify_failure('failed', 1, echoed), 'other')

    def test_cli_missing(self) -> None:
        self.assertEqual(classify_failure('failed', 127, "not found in PATH"), 'cli_missing')
        self.assertEqual(classify_failure('failed', 126, ''), 'cli_missing')

    def test_spawn_hint_other_beats_exit_127(self) -> None:
        self.assertEqual(classify_failure('failed', 127, 'bad payload', hint='other'), 'other')

    def test_timeout(self) -> None:
        self.assertEqual(classify_failure('timeout', 124, AUTH_STDERR), 'timeout')
        self.assertEqual(classify_failure('failed', 124, ''), 'timeout')

    def test_result_is_always_bounded_enum(self) -> None:
        for args in (('failed', None, None), ('failed', 'x', object()),
                     ('weird', 2, 'b' * 100000)):
            self.assertIn(classify_failure(*args), FAILURE_CLASSES)


class Threshold(unittest.TestCase):
    def test_resolution(self) -> None:
        self.assertEqual(resolve_threshold(None, ''), 3)
        self.assertEqual(resolve_threshold(None, '2'), 2)
        self.assertEqual(resolve_threshold(None, '0'), 0)
        self.assertEqual(resolve_threshold(None, 'junk'), 3)
        self.assertEqual(resolve_threshold(0, '5'), 0)
        self.assertEqual(resolve_threshold(True, '5'), 5)

    def test_trailing_failures(self) -> None:
        history = [{'status': 'failed'}, {'status': 'success'}, {'status': 'failed'},
                   'junk', {'status': 'timeout'}]
        self.assertEqual(trailing_failures(history), 2)


def stamp(minutes: int) -> str:
    return f'2026-01-{1 + minutes // 1440:02d}T{(minutes // 60) % 24:02d}:{minutes % 60:02d}:00Z'


class TaskTransition(unittest.TestCase):
    def drive(self, classes: list[str | None], start: int = 0, prev=None):
        state, reasons = prev, []
        for offset, cls in enumerate(classes):
            state, event = task_transition(state, failed=cls is not None, failure_class=cls,
                                           threshold=3, at=stamp(start + offset))
            reasons.append(event and event['reason'])
        return state, reasons

    def test_alerts_once_per_streak(self) -> None:
        _state, reasons = self.drive(['auth_failed'] * 8)
        self.assertEqual(reasons.count('threshold'), 1)
        self.assertEqual(reasons, [None, None, 'threshold'] + [None] * 5)

    def test_flapping_exit_codes_do_not_realert_within_24h(self) -> None:
        _state, reasons = self.drive(['other', 'cli_missing'] * 10)
        self.assertEqual([r for r in reasons if r], ['threshold'])

    def test_flapping_timeout_other_never_realerts(self) -> None:
        state, reasons = self.drive(['timeout', 'other'] * 5)
        self.assertEqual([r for r in reasons if r], ['threshold'])
        _state, reasons = self.drive(['timeout', 'other'] * 5, start=3000, prev=state)
        self.assertEqual([r for r in reasons if r], [])

    def test_escalation_to_auth_realerts_at_most_once_per_24h(self) -> None:
        state, reasons = self.drive(['other'] * 3 + ['auth_failed'])
        self.assertEqual(reasons, [None, None, 'threshold', None])  # cooldown
        state, reasons = self.drive(['auth_failed'], start=3 + 1440, prev=state)
        self.assertEqual(reasons, ['class-change'])
        state, reasons = self.drive(['cli_missing', 'auth_failed'], start=3 + 1441, prev=state)
        self.assertEqual(reasons, [None, None])

    def test_recovery_once(self) -> None:
        _state, reasons = self.drive(['other'] * 3 + [None, None])
        self.assertEqual(reasons, [None, None, 'threshold', 'recovered', None])

    def test_seed(self) -> None:
        _state, event = task_transition(None, failed=True, failure_class='auth_failed',
                                        threshold=3, at=stamp(0), seed_streak=4)
        self.assertEqual((event['reason'], event['consecutiveFailures']), ('threshold', 4))


class NodeTransition(unittest.TestCase):
    def drive(self, runs: list[tuple[str, str | None]], prev=None):
        state, reasons = prev, []
        for offset, (task_id, cls) in enumerate(runs):
            state, event = node_transition(state, task_id=task_id, failed=cls is not None,
                                           failure_class=cls, threshold=3, at=stamp(offset))
            reasons.append(event and event['reason'])
        return state, reasons

    def test_needs_two_distinct_tasks(self) -> None:
        _state, reasons = self.drive([('a', 'auth_failed')] * 6)
        self.assertEqual([r for r in reasons if r], [])

    def test_once_per_streak_even_with_class_changes(self) -> None:
        _state, reasons = self.drive([('a', 'auth_failed'), ('b', 'other')] * 5)
        self.assertEqual([r for r in reasons if r], ['threshold'])

    def test_non_member_success_neither_clears_nor_announces(self) -> None:
        state, reasons = self.drive([('a', 'auth_failed'), ('b', 'auth_failed'),
                                     ('c', 'auth_failed'), ('healthy', None)])
        self.assertEqual(reasons, [None, None, 'threshold', None])
        _state, reasons = self.drive([('b', None)], prev=state)
        self.assertEqual(reasons, ['recovered'])

    def test_alerted_streak_with_no_runnable_member_resets_silently(self) -> None:
        state, reasons = self.drive([('a', 'auth_failed'), ('b', 'auth_failed'),
                                     ('c', 'auth_failed')])
        self.assertEqual(reasons, [None, None, 'threshold'])
        # a, b, c can never run again; a healthy d succeeding clears silently.
        state, event = node_transition(state, task_id='d', failed=False, failure_class=None,
                                       threshold=3, at=stamp(10), runnable_ids={'d', 'e', 'f'})
        self.assertIsNone(event)
        self.assertFalse(state['alerted'])
        self.assertEqual((state['consecutiveFailures'], state['taskIds']), (0, []))

    def test_pruned_but_nonempty_alerted_streak_stays_latched(self) -> None:
        state, _reasons = self.drive([('a', 'other'), ('b', 'other'), ('c', 'other')])
        state, event = node_transition(state, task_id='x', failed=True, failure_class='other',
                                       threshold=3, at=stamp(10), runnable_ids={'b', 'x'})
        self.assertIsNone(event)
        self.assertTrue(state['alerted'])
        self.assertEqual(state['taskIds'], ['b', 'x'])
        _state, event = node_transition(state, task_id='b', failed=False, failure_class=None,
                                        threshold=3, at=stamp(11), runnable_ids={'b', 'x'})
        self.assertEqual(event['reason'], 'recovered')

    def test_unalerted_streak_is_not_pruned(self) -> None:
        # Finished one-shots that each failed once are the incident itself.
        state = None
        for offset, tid in enumerate(('a', 'b', 'c')):
            state, event = node_transition(state, task_id=tid, failed=True,
                                           failure_class='auth_failed', threshold=3,
                                           at=stamp(offset), runnable_ids=set())
        self.assertEqual(event['reason'], 'threshold')

    def test_no_recovery_notice_without_alert(self) -> None:
        _state, reasons = self.drive([('a', 'other'), ('b', 'other'), ('a', None)])
        self.assertEqual([r for r in reasons if r], [])


class StaleTransition(unittest.TestCase):
    """#1821 proposal 1: no prompt success for D days -> one node alarm."""

    def step(self, prev, *, failed=True, at='2026-01-08T00:00:00Z',
             last_success='2026-01-01T00:00:00Z', first_run=None, days=7):
        return stale_transition(prev, failed=failed, at=at, last_success=last_success,
                                first_run=first_run, days=days)

    def test_days_resolution(self) -> None:
        self.assertEqual(resolve_stale_days(''), 7)
        self.assertEqual(resolve_stale_days('0'), 0)
        self.assertEqual(resolve_stale_days('3'), 3)
        self.assertEqual(resolve_stale_days('junk'), 7)
        self.assertEqual(resolve_stale_days('99999'), 366)

    def test_alerts_once_at_the_threshold(self) -> None:
        state, event = self.step({}, at='2026-01-07T23:59:00Z')
        self.assertIsNone(event)
        state, event = self.step(state)
        self.assertEqual(event['reason'], 'stale')
        self.assertEqual((event['ageDays'], event['basis']), (7, 'last-success'))
        state, event = self.step(state, at='2026-01-20T00:00:00Z')
        self.assertIsNone(event)

    def test_success_clears_and_a_new_stretch_alerts_again(self) -> None:
        state, _ = self.step({})
        state, event = self.step(state, failed=False, at='2026-01-09T00:00:00Z')
        self.assertIsNone(event)
        self.assertNotIn('staleAlertedAt', state)
        _state, event = self.step(state, at='2026-01-17T00:00:00Z',
                                  last_success='2026-01-09T00:00:00Z')
        self.assertEqual(event['ageDays'], 8)

    def test_unseen_later_success_unlatches(self) -> None:
        state, _ = self.step({})
        _state, event = self.step(state, at='2026-01-17T00:00:00Z',
                                  last_success='2026-01-09T00:00:00Z')
        self.assertEqual(event['reason'], 'stale')

    def test_no_success_uses_the_first_recorded_run(self) -> None:
        _state, event = self.step({}, last_success=None, first_run='2026-01-01T00:00:00Z')
        self.assertEqual(event['basis'], 'no-success-since-first-run')
        _state, event = self.step({}, last_success=None, first_run=None)
        self.assertIsNone(event)

    def test_disabled_and_bad_stamps_never_alert(self) -> None:
        self.assertIsNone(self.step({}, days=0)[1])
        self.assertIsNone(self.step({}, at='not-a-time')[1])

    def test_text_names_the_stretch_and_the_opt_out(self) -> None:
        event = {'reason': 'stale', 'days': 7, 'ageDays': 9, 'basis': 'last-success',
                 'since': '2026-01-01T00:00:00Z'}
        text = alarm_text('weekly', None, None, None, stale_event=event, failure_class='other')
        self.assertIn('no prompt task has succeeded for 9d (threshold 7d)', text)
        self.assertIn('last prompt success: 2026-01-01T00:00:00Z', text)
        self.assertIn('CCC_AGENT_CRON_PROMPT_STALE_DAYS=0', text)


class AlarmText(unittest.TestCase):
    def test_task_auth_hint_does_not_claim_every_prompt_task(self) -> None:
        event = {'reason': 'threshold', 'consecutiveFailures': 3, 'failureClass': 'auth_failed'}
        text = alarm_text('probe', event, None, None)
        self.assertIn('task probe failed 3 consecutive runs, class=auth_failed', text)
        self.assertNotIn('every prompt task', text)

    def test_node_text_names_tasks(self) -> None:
        event = {'reason': 'threshold', 'consecutiveFailures': 3, 'failureClass': 'auth_failed',
                 'taskIds': ['a', 'b', 'c']}
        text = alarm_text('c', None, event, '2026-08-05T00:00:00Z')
        self.assertIn('across 3 tasks (a, b, c)', text)
        self.assertIn('last prompt success: 2026-08-05T00:00:00Z', text)


FAKE_RUNNER = textwrap.dedent('''
    import json, os, sys
    modes = json.load(open(os.environ["FAKE_MODES"]))
    mode = modes.get(sys.argv[-1], "ok")
    if mode == "ok":
        print("fine"); sys.exit(0)
    if mode == "auth":
        sys.stderr.write(%r); sys.exit(1)
    codes = {"exit1": 1, "exit127": 127, "exit124": 124}
    sys.stderr.write("boom\\n"); sys.exit(codes[mode])
''') % AUTH_STDERR


class Sandbox:
    """Temporary HOME/store/spool; the environment is built from scratch."""

    def __init__(self, task_ids: list[str], extra_tasks: list[dict] | None = None) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.store = self.root / 'home' / 'state' / 'tasks.json'
        self.store.parent.mkdir(parents=True)
        self.spool = self.root / 'spool'
        self.modes = self.root / 'modes.json'
        runner = self.root / 'fake_runner.py'
        runner.write_text(FAKE_RUNNER, encoding='utf-8')
        tasks = [{'id': tid, 'schedule': '* * * * *', 'prompt': tid, 'enabled': True,
                  'notify': 'none', 'lastRunAt': '2026-01-01T00:00:00Z'} for tid in task_ids]
        tasks += extra_tasks or []
        self.store.write_text(json.dumps({'version': 1, 'tasks': tasks}), encoding='utf-8')
        self.env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'TMPDIR': str(self.root),
            'HOME': str(self.root / 'home'),
            'LANG': 'C.UTF-8',
            'CCC_AGENT_CRON_STORE': str(self.store),
            'CCC_PUSH_SPOOL': str(self.spool),
            'CCC_HEADLESS_CMD': f'{sys.executable} {runner}',
            'FAKE_MODES': str(self.modes),
        }
        self.minute = 0

    def close(self) -> None:
        self._dir.cleanup()

    def run(self, task_id: str, mode: str, advance: int = 1) -> dict:
        self.minute += advance
        self.modes.write_text(json.dumps({task_id: mode}), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, str(HERE / 'agent_cron.py'), 'run', task_id, '--json',
             '--at', stamp(self.minute)],
            env=self.env, capture_output=True, text=True, timeout=120,
        )
        result = json.loads(proc.stdout)
        result['_stderr'] = proc.stderr
        return result

    def alarms(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.spool.glob('*-alarm.json'))]

    def alarm_state(self) -> dict:
        return json.loads((self.store.parent / 'failure-alarm.json').read_text())


class Integration(unittest.TestCase):
    def sandbox(self, task_ids: list[str], **kwargs) -> Sandbox:
        box = Sandbox(task_ids, **kwargs)
        self.addCleanup(box.close)
        return box

    def test_two_tasks_with_different_classes_do_not_storm(self) -> None:
        # Review finding 1: 8 alarms in 10 runs before the fix.
        box = self.sandbox(['a', 'b'])
        for _ in range(5):
            box.run('a', 'auth')
            box.run('b', 'exit1')
        reasons = [r for alarm in box.alarms() for r in alarm['reasons']]
        self.assertEqual(sorted(reasons), ['node=threshold', 'task=threshold', 'task=threshold'])
        self.assertLessEqual(len(box.alarms()), 3)

    def test_auth_and_cli_failures_skip_the_backoff_retry(self) -> None:
        # #1821: with attempts left, auth_failed / cli_missing record no retry;
        # a generic failure still schedules one.
        policy = {'retryPolicy': {'maxAttempts': 3, 'backoffSec': 60}}
        extra = [{'id': tid, 'schedule': '* * * * *', 'prompt': tid, 'enabled': True,
                  'notify': 'none', 'lastRunAt': '2026-01-01T00:00:00Z', **policy}
                 for tid in ('auth', 'cli', 'generic')]
        box = self.sandbox([], extra_tasks=extra)
        for tid, mode in (('auth', 'auth'), ('cli', 'exit127'), ('generic', 'exit1')):
            box.run(tid, mode)
        tasks = {t['id']: t for t in json.loads(box.store.read_text())['tasks']}
        for tid in ('auth', 'cli'):
            state = tasks[tid]['retryState']
            self.assertEqual(state['lastStatus'], 'not-retryable', tid)
            self.assertIsNone(state['retryEligibleAt'], tid)
        self.assertIsNotNone(tasks['generic']['retryState']['retryEligibleAt'])
        self.assertEqual(box.alarm_state()['tasks']['auth']['failureClass'], 'auth_failed')

    def _no_retry_task(self, tid: str, schedule: str, **extra) -> dict:
        task = {'id': tid, 'schedule': schedule, 'prompt': tid, 'enabled': True, 'notify': 'none',
                'retryPolicy': {'maxAttempts': 3, 'backoffSec': 60}}
        task.update(extra)
        return task

    def test_skipped_retry_alerts_on_the_first_failure(self) -> None:
        # #1821 review F1: retries were what reached the threshold within
        # minutes. Without them a daily task would alert days later and a
        # one-shot never, so a not-retryable prompt failure alerts at once.
        extra = [self._no_retry_task('daily', '0 0 * * *', lastRunAt='2025-12-31T00:00:00Z'),
                 self._no_retry_task('once', 'at 2026-01-01T00:00:00Z')]
        box = self.sandbox([], extra_tasks=extra)
        box.run('daily', 'auth', advance=0)
        box.run('once', 'exit127', advance=0)
        alarms = {a['taskId']: a for a in box.alarms()}
        self.assertEqual(sorted(alarms), ['daily', 'once'])
        for tid in ('daily', 'once'):
            self.assertIn('task=threshold', alarms[tid]['reasons'])
        state = box.alarm_state()['tasks']
        self.assertEqual(state['daily']['alertedClass'], 'auth_failed')
        self.assertEqual(state['once']['alertedClass'], 'cli_missing')
        # Deduped: the next occurrence's failure raises no second task alarm
        # (the node-wide counter may still fire on its own 3-run threshold).
        box.run('daily', 'auth', advance=1440)
        task_alarms = [a for a in box.alarms()
                       if a['taskId'] == 'daily' and 'task=threshold' in a['reasons']]
        self.assertEqual(len(task_alarms), 1)

    def test_opted_out_task_stays_silent_even_without_retry(self) -> None:
        extra = [self._no_retry_task('quiet', '* * * * *', lastRunAt='2026-01-01T00:00:00Z',
                                     failureAlertAfter=0)]
        box = self.sandbox([], extra_tasks=extra)
        box.run('quiet', 'auth')
        self.assertEqual(box.alarms(), [])

    def test_command_task_auth_stderr_keeps_its_retry(self) -> None:
        # #1821 review F2: a command task's own stderr (a 401 from a service
        # that is restarting) is not proof of a permanent credential failure.
        script = 'import sys; sys.stderr.write("HTTP 401 Unauthorized\\n"); sys.exit(1)'
        extra = [self._no_retry_task('cmd', '* * * * *', lastRunAt='2026-01-01T00:00:00Z',
                                     payload={'kind': 'command', 'argv': [sys.executable, '-c', script]})]
        box = self.sandbox([], extra_tasks=extra)
        box.run('cmd', 'ok')
        task = json.loads(box.store.read_text())['tasks'][0]
        self.assertEqual(box.alarm_state()['tasks']['cmd']['failureClass'], 'auth_failed')
        self.assertIsNotNone(task['retryState']['retryEligibleAt'])
        self.assertEqual(box.alarms(), [])

    def _sparse_task(self, tid: str) -> dict:
        # A weekly prompt task whose last success is 8 days back: the
        # consecutive counters (threshold 3) would need weeks to fire.
        old = {'runId': 'old', 'scheduledAt': '2025-12-24T00:00:00Z',
               'startedAt': '2025-12-24T00:00:00Z', 'finishedAt': '2025-12-24T00:00:00Z',
               'status': 'success', 'exitCode': 0, 'attempt': 1, 'notifyState': 'none'}
        return {'id': tid, 'schedule': '* * * * *', 'prompt': tid, 'enabled': True,
                'notify': 'none', 'lastRunAt': '2026-01-01T00:00:00Z', 'runHistory': [old]}

    def test_prompt_stale_alarm_fires_once_and_rearms_after_success(self) -> None:
        box = self.sandbox([], extra_tasks=[self._sparse_task('weekly')])
        first = box.run('weekly', 'exit1')
        self.assertEqual(first['failureAlarm']['reasons'], ['node=stale'])
        self.assertIn('no prompt task has succeeded for 8d', box.alarms()[0]['text'])
        second = box.run('weekly', 'exit1')
        self.assertEqual(second['failureAlarm']['state'], 'no-alert')
        box.run('weekly', 'ok')
        self.assertNotIn('staleAlertedAt', box.alarm_state()['node'])
        box.run('weekly', 'exit1', advance=7 * 1440)
        self.assertEqual(len([a for a in box.alarms() if 'node=stale' in a['reasons']]), 2)

    def test_prompt_stale_alarm_respects_the_opt_outs(self) -> None:
        box = self.sandbox([], extra_tasks=[self._sparse_task('weekly')])
        box.env['CCC_AGENT_CRON_PROMPT_STALE_DAYS'] = '0'
        self.assertEqual(box.run('weekly', 'exit1')['failureAlarm']['state'], 'no-alert')
        quiet = {**self._sparse_task('quiet'), 'failureAlertAfter': 0}
        box = self.sandbox([], extra_tasks=[quiet])
        self.assertEqual(box.run('quiet', 'exit1')['failureAlarm']['state'], 'no-alert')

    def test_first_ever_prompt_failure_is_not_stale(self) -> None:
        box = self.sandbox(['fresh'])
        self.assertEqual(box.run('fresh', 'exit1')['failureAlarm']['state'], 'no-alert')

    def test_per_task_class_flapping_is_bounded(self) -> None:
        # Review finding 2: exit 1 <-> 127 alerted on every run.
        box = self.sandbox(['flappy'])
        for _ in range(5):
            box.run('flappy', 'exit1')
            box.run('flappy', 'exit127')
        self.assertEqual(len(box.alarms()), 1)
        box.run('flappy', 'exit127', advance=25 * 60)
        self.assertEqual(len(box.alarms()), 2)
        self.assertEqual(box.alarms()[-1]['reasons'], ['task=class-change'])
        for _ in range(4):
            box.run('flappy', 'exit124')
            box.run('flappy', 'exit1')
        self.assertEqual(len(box.alarms()), 2)

    def test_one_broken_one_healthy_task_is_one_alarm_total(self) -> None:
        # Review finding 3: alarm/"cleared" pair every cycle forever.
        box = self.sandbox(['broken', 'healthy'])
        for _ in range(10):
            box.run('broken', 'auth')
            box.run('healthy', 'ok')
        alarms = box.alarms()
        self.assertEqual([a['reasons'] for a in alarms], [['task=threshold']])
        self.assertNotIn('every prompt task', alarms[0]['text'])
        self.assertNotIn('node', alarms[0]['text'].split('\n')[0])

    def test_state_write_failure_fails_quiet(self) -> None:
        # Review finding 5: spool-before-save alerted on every run.
        box = self.sandbox(['a'])
        (box.store.parent / 'failure-alarm.json').mkdir()
        results = [box.run('a', 'auth') for _ in range(6)]
        self.assertEqual(box.alarms(), [])
        self.assertTrue(all(r['failureAlarm']['state'] == 'state-write-failed' for r in results))
        self.assertTrue(results[-1]['failureAlarm']['alertSuppressed'])
        self.assertIn('alert suppressed', results[-1]['_stderr'])

    def test_incident_shape_alerts_once_and_recovers_once(self) -> None:
        box = self.sandbox(['t1', 't2', 't3', 't4', 'other-ok'])
        for tid in ('t1', 't2', 't3', 't4'):
            box.run(tid, 'auth')
        self.assertEqual([a['reasons'] for a in box.alarms()], [['node=threshold']])
        self.assertIn('across 3 tasks (t1, t2, t3)', box.alarms()[0]['text'])
        box.run('other-ok', 'ok')  # not part of the streak: stays silent
        self.assertEqual(len(box.alarms()), 1)
        box.run('t2', 'ok')
        self.assertEqual(box.alarms()[-1]['reasons'], ['node=recovered'])
        box.run('t3', 'ok')
        self.assertEqual(len(box.alarms()), 2)

    @staticmethod
    def one_shots(*ids: str) -> list[dict]:
        return [{'id': tid, 'schedule': 'at 2026-01-01T00:01:00Z', 'prompt': tid,
                 'enabled': True, 'notify': 'none'} for tid in ids]

    def test_node_alarm_does_not_latch_after_alerted_tasks_finish(self) -> None:
        # Re-review repro: one-shots a,b,c each fail once -> node alarm; they
        # can never run again; healthy d succeeds; later e,f,g fail once each
        # and e,f fail again -> a second node alarm must fire, silently reset.
        box = self.sandbox(['d', 'e', 'f', 'g'], extra_tasks=self.one_shots('a', 'b', 'c'))
        for tid in ('a', 'b', 'c'):
            box.run(tid, 'auth')
        self.assertEqual([a['reasons'] for a in box.alarms()], [['node=threshold']])
        box.run('d', 'ok')
        self.assertEqual(len(box.alarms()), 1)  # silent reset: no "cleared" message
        node = box.alarm_state()['node']
        self.assertEqual((node['alerted'], node['taskIds']), (False, []))
        for tid in ('e', 'f', 'g', 'e', 'f'):
            box.run(tid, 'auth')
        alarms = box.alarms()
        self.assertEqual([a['reasons'] for a in alarms], [['node=threshold'], ['node=threshold']])
        self.assertIn('across 3 tasks (e, f, g)', alarms[1]['text'])
        self.assertFalse(any('cleared' in a['text'] for a in alarms))

    def test_node_alarm_resets_without_a_healthy_run_too(self) -> None:
        box = self.sandbox(['e', 'f', 'g'], extra_tasks=self.one_shots('a', 'b', 'c'))
        for tid in ('a', 'b', 'c', 'e', 'f', 'g'):
            box.run(tid, 'auth')
        alarms = box.alarms()
        self.assertEqual([a['reasons'] for a in alarms], [['node=threshold'], ['node=threshold']])
        self.assertFalse(any('cleared' in a['text'] for a in alarms))

    def test_opted_out_task_is_excluded_from_node_counter(self) -> None:
        box = self.sandbox(['a'], extra_tasks=[
            {'id': 'quiet', 'schedule': '* * * * *', 'prompt': 'quiet', 'enabled': True,
             'notify': 'none', 'failureAlertAfter': 0, 'lastRunAt': '2026-01-01T00:00:00Z'}])
        for _ in range(3):
            box.run('quiet', 'auth')
            box.run('a', 'auth')
        # 'a' alone reaches its own threshold; 'quiet' never counts anywhere.
        self.assertEqual([a['reasons'] for a in box.alarms()], [['task=threshold']])
        self.assertEqual(box.alarms()[0]['taskId'], 'a')

    def test_task_store_stays_valid_under_pre_1821_schema(self) -> None:
        # Review finding 4: reverting must not brick the scheduler.
        box = self.sandbox(['a', 'b'])
        for mode in ('auth', 'exit127', 'ok', 'exit124'):
            box.run('a', mode)
            box.run('b', mode)
        old_schema = json.loads((HERE / 'testdata' /
                                 'agent-cron-task-store.pre-1821.schema.json').read_text())
        store = json.loads(box.store.read_text())
        self.assertEqual(agent_cron_schema._validate_node(store, old_schema, ''), [])
        self.assertNotIn('failureClass', box.store.read_text())
        self.assertNotIn('lastSuccessAt', box.store.read_text())
        # Acceptance (a): the per-run class lives in the alarm state instead.
        runs = box.alarm_state()['tasks']['a']['runs']
        self.assertEqual([r.get('failureClass') for r in runs],
                         ['auth_failed', 'cli_missing', None, 'timeout'])
        self.assertEqual(box.alarm_state()['tasks']['a']['lastSuccessAt'], stamp(5))
        self.assertNotIn('boom', json.dumps(box.alarm_state()))


if __name__ == '__main__':
    unittest.main()
