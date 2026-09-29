#!/usr/bin/env python3
"""Unit tests for agent-cron failure classification and alarm transitions (#1821).

The incident: a node's claude login broke and prompt tasks failed for six days
(`exited 1` + `"error":"authentication_failed"`) with no alarm. These tests pin
the bounded failure class and the dedupe contract: one alert at the Nth
consecutive failure, a repeat only on class change, one recovery notice.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cron_alarm import (  # noqa: E402
    FAILURE_CLASSES,
    NODE_PROMPT_KEY,
    alarm_text,
    alarm_transition,
    classify_failure,
    resolve_threshold,
    trailing_failures,
)

AUTH_STDERR = (
    'ccc-headless: /usr/bin/claude exited 1\n'
    'ccc-headless: stdout (first 2000B of 180B):\n'
    '{"type":"result","is_error":true,"error":"authentication_failed"}'
)


class ClassifyFailure(unittest.TestCase):
    def test_success_has_no_class(self) -> None:
        self.assertIsNone(classify_failure('success', 0, '', AUTH_STDERR))

    def test_observed_auth_failure(self) -> None:
        self.assertEqual(classify_failure('failed', 1, '', AUTH_STDERR), 'auth_failed')

    def test_auth_variants(self) -> None:
        for text in ('Not logged in · Please run /login', 'Invalid API key',
                     'OAuth token has expired', 'HTTP 401 Unauthorized',
                     '{"type":"authentication_error"}'):
            with self.subTest(text=text):
                self.assertEqual(classify_failure('failed', 1, text, ''), 'auth_failed')

    def test_cli_missing(self) -> None:
        self.assertEqual(classify_failure('failed', 127, '', "claude: not found in PATH"), 'cli_missing')
        self.assertEqual(classify_failure('failed', 126, '', ''), 'cli_missing')
        self.assertEqual(classify_failure('failed', 127, '', '', hint='cli_missing'), 'cli_missing')

    def test_spawn_hint_other_beats_exit_127(self) -> None:
        # run_execute reports every pre-spawn exception as exit 127; a payload
        # error is not a missing CLI.
        self.assertEqual(classify_failure('failed', 127, '', 'bad payload', hint='other'), 'other')

    def test_timeout(self) -> None:
        self.assertEqual(classify_failure('timeout', 124, '', AUTH_STDERR), 'timeout')
        self.assertEqual(classify_failure('failed', 124, '', ''), 'timeout')

    def test_other(self) -> None:
        self.assertEqual(classify_failure('failed', 7, 'fake failure', ''), 'other')

    def test_result_is_always_bounded_enum(self) -> None:
        for args in (('failed', None, None, None), ('failed', 'x', 5, object()),
                     ('weird', 2, 'a' * 100000, 'b' * 100000)):
            self.assertIn(classify_failure(*args), FAILURE_CLASSES)


class Threshold(unittest.TestCase):
    def test_default_is_three(self) -> None:
        self.assertEqual(resolve_threshold(None, ''), 3)

    def test_env_override_and_disable(self) -> None:
        self.assertEqual(resolve_threshold(None, '2'), 2)
        self.assertEqual(resolve_threshold(None, '0'), 0)
        self.assertEqual(resolve_threshold(None, 'junk'), 3)

    def test_task_value_wins(self) -> None:
        self.assertEqual(resolve_threshold(0, '5'), 0)
        self.assertEqual(resolve_threshold(2, '5'), 2)
        self.assertEqual(resolve_threshold(True, '5'), 5)


def drive(outcomes, threshold=3, prev=None, seed=0):
    """Feed (failed, class) outcomes; mark alerts delivered like the caller."""
    state, events = prev, []
    for index, (failed, cls) in enumerate(outcomes):
        state, event = alarm_transition(state, failed=failed, failure_class=cls,
                                        threshold=threshold, at=f't{index}', seed_streak=seed)
        if event and event['reason'] != 'recovered':
            state['alertedClass'] = event['failureClass']
        events.append(event and event['reason'])
    return state, events


class AlarmTransition(unittest.TestCase):
    def test_alerts_once_at_threshold(self) -> None:
        _state, events = drive([(True, 'auth_failed')] * 6)
        self.assertEqual(events, [None, None, 'threshold', None, None, None])

    def test_class_change_realerts_once(self) -> None:
        _state, events = drive([(True, 'auth_failed')] * 3 + [(True, 'timeout')] * 2)
        self.assertEqual(events, [None, None, 'threshold', 'class-change', None])

    def test_recovery_after_alert(self) -> None:
        state, events = drive([(True, 'other')] * 3 + [(False, None)] + [(True, 'other')] * 3)
        self.assertEqual(events, [None, None, 'threshold', 'recovered', None, None, 'threshold'])
        self.assertEqual(state['consecutiveFailures'], 3)

    def test_success_without_alert_is_silent(self) -> None:
        _state, events = drive([(True, 'other')] * 2 + [(False, None)] + [(True, 'other')] * 2)
        self.assertEqual(events, [None] * 5)

    def test_disabled_threshold_never_fires(self) -> None:
        _state, events = drive([(True, 'auth_failed')] * 10, threshold=0)
        self.assertEqual(events, [None] * 10)

    def test_undelivered_alert_retries_next_run(self) -> None:
        state, event = alarm_transition(None, failed=True, failure_class='other',
                                        threshold=1, at='t0')
        self.assertEqual(event['reason'], 'threshold')
        # Caller did not mark alertedClass (spool failed): next failure retries.
        _state, event = alarm_transition(state, failed=True, failure_class='other',
                                         threshold=1, at='t1')
        self.assertEqual(event['reason'], 'threshold')

    def test_seed_from_history_on_first_state(self) -> None:
        _state, event = alarm_transition(None, failed=True, failure_class='auth_failed',
                                         threshold=3, at='t', seed_streak=4)
        self.assertEqual(event['reason'], 'threshold')
        self.assertEqual(event['consecutiveFailures'], 4)

    def test_trailing_failures(self) -> None:
        history = [{'status': 'failed'}, {'status': 'success'}, {'status': 'failed'},
                   'junk', {'status': 'timeout'}]
        self.assertEqual(trailing_failures(history), 2)
        self.assertEqual(trailing_failures(None), 0)


class AlarmText(unittest.TestCase):
    def test_text_names_class_and_node_scope(self) -> None:
        events = {
            'task': {'reason': 'threshold', 'consecutiveFailures': 3, 'failureClass': 'auth_failed'},
            NODE_PROMPT_KEY: {'reason': 'threshold', 'consecutiveFailures': 3,
                              'failureClass': 'auth_failed'},
        }
        text = alarm_text('probe', events, '2026-08-05T00:00:00Z')
        self.assertIn('class=auth_failed', text)
        self.assertIn('task probe failed 3 consecutive runs', text)
        self.assertIn('last prompt success: 2026-08-05T00:00:00Z', text)
        self.assertIn('re-authenticated', text)
        self.assertIn('failureAlertAfter=0', text)

    def test_recovery_text(self) -> None:
        events = {'task': {'reason': 'recovered', 'consecutiveFailures': 4, 'failureClass': 'other'}}
        text = alarm_text('probe', events, None)
        self.assertIn('cleared: task probe succeeded after 4 consecutive failures', text)
        self.assertNotIn('failureAlertAfter', text)


if __name__ == '__main__':
    unittest.main()
