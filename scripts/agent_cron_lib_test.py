#!/usr/bin/env python3
"""Direct unit tests for agent_cron_lib (pure schedule + retry helpers).

Run standalone: python3 scripts/agent_cron_lib_test.py
These exercise the deterministic cron/retry math that previously lived inline in
agent_cron.py and had only indirect CLI-level coverage via agent-cron.test.sh.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_cron_lib as lib


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class ParseDtTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(lib.parse_dt(None))
        self.assertIsNone(lib.parse_dt(''))

    def test_z_suffix_normalized_to_utc(self):
        self.assertEqual(lib.parse_dt('2026-06-28T10:30:45Z'), _dt(2026, 6, 28, 10, 30))

    def test_naive_assumed_utc_and_truncates_seconds(self):
        self.assertEqual(lib.parse_dt('2026-06-28T10:30:45'), _dt(2026, 6, 28, 10, 30))

    def test_offset_converted_to_utc(self):
        self.assertEqual(lib.parse_dt('2026-06-28T12:30:00+02:00'), _dt(2026, 6, 28, 10, 30))

    def test_non_string_raises(self):
        with self.assertRaises(ValueError):
            lib.parse_dt(12345)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            lib.parse_dt('not-a-date')


class ExpandFieldTest(unittest.TestCase):
    def test_star(self):
        self.assertEqual(lib.expand_field('*', 0, 3), {0, 1, 2, 3})

    def test_step(self):
        self.assertEqual(lib.expand_field('*/15', 0, 59), {0, 15, 30, 45})

    def test_list(self):
        self.assertEqual(lib.expand_field('1,3,5', 0, 10), {1, 3, 5})

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            lib.expand_field('99', 0, 59)

    def test_zero_step_raises(self):
        with self.assertRaises(ValueError):
            lib.expand_field('*/0', 0, 59)


class ParseScheduleTest(unittest.TestCase):
    def test_shorthand_daily(self):
        spec = lib.parse_schedule('@daily')
        self.assertEqual(spec['minute'], {0})
        self.assertEqual(spec['hour'], {0})
        self.assertEqual(spec['expr'], '0 0 * * *')

    def test_five_field(self):
        spec = lib.parse_schedule('30 9 * * 1')
        self.assertEqual(spec['minute'], {30})
        self.assertEqual(spec['hour'], {9})
        self.assertEqual(spec['dow'], {1})

    def test_reboot_rejected(self):
        with self.assertRaises(ValueError):
            lib.parse_schedule('@reboot')

    def test_wrong_field_count(self):
        with self.assertRaises(ValueError):
            lib.parse_schedule('* * *')

    def test_bad_field(self):
        with self.assertRaises(ValueError):
            lib.parse_schedule('60 * * * x')


class CronMatchesTest(unittest.TestCase):
    def test_minute_hour_match(self):
        spec = lib.parse_schedule('30 9 * * *')
        self.assertTrue(lib.cron_matches(_dt(2026, 6, 28, 9, 30), spec))
        self.assertFalse(lib.cron_matches(_dt(2026, 6, 28, 9, 31), spec))

    def test_sunday_zero_or_seven(self):
        # 2026-06-28 is a Sunday.
        spec7 = lib.parse_schedule('0 0 * * 7')
        spec0 = lib.parse_schedule('0 0 * * 0')
        self.assertTrue(lib.cron_matches(_dt(2026, 6, 28, 0, 0), spec7))
        self.assertTrue(lib.cron_matches(_dt(2026, 6, 28, 0, 0), spec0))

    def test_dom_or_dow_when_both_restricted(self):
        # Either field may match when both DOM and DOW are restricted.
        spec = lib.parse_schedule('0 0 1 * 1')  # 1st of month OR Monday
        self.assertTrue(lib.cron_matches(_dt(2026, 6, 1, 0, 0), spec))   # 1st (Monday too)
        self.assertTrue(lib.cron_matches(_dt(2026, 6, 8, 0, 0), spec))   # Monday, not 1st
        self.assertTrue(lib.cron_matches(_dt(2026, 6, 15, 0, 0), spec))  # Monday
        self.assertFalse(lib.cron_matches(_dt(2026, 6, 9, 0, 0), spec))  # Tuesday, not 1st


class OccurrenceTest(unittest.TestCase):
    def test_next_occurrence_hourly(self):
        spec = lib.parse_schedule('0 * * * *')
        nxt = lib.next_occurrence(spec, _dt(2026, 6, 28, 9, 15))
        self.assertEqual(nxt, _dt(2026, 6, 28, 10, 0))

    def test_iter_occurrences_inclusive_window(self):
        spec = lib.parse_schedule('0 * * * *')
        out, truncated = lib.iter_occurrences(
            spec, _dt(2026, 6, 28, 8, 30), _dt(2026, 6, 28, 11, 0)
        )
        self.assertEqual(out, [_dt(2026, 6, 28, 9, 0), _dt(2026, 6, 28, 10, 0), _dt(2026, 6, 28, 11, 0)])
        self.assertFalse(truncated)

    def test_iter_occurrences_cap_truncates(self):
        spec = lib.parse_schedule('* * * * *')  # every minute
        out, truncated = lib.iter_occurrences(
            spec, _dt(2026, 6, 28, 0, 0), _dt(2026, 6, 28, 1, 0), cap=5
        )
        self.assertEqual(len(out), 5)
        self.assertTrue(truncated)


class RetryPolicyTest(unittest.TestCase):
    def test_defaults(self):
        p = lib.retry_policy({})
        self.assertEqual(p, {'maxAttempts': 1, 'backoffSec': 60, 'backoffMultiplier': 2, 'maxBackoffSec': 3600})

    def test_clamps_out_of_range(self):
        p = lib.retry_policy({'retryPolicy': {'maxAttempts': 999, 'backoffSec': -5}})
        self.assertEqual(p['maxAttempts'], 1)   # out of [1,10] -> default
        self.assertEqual(p['backoffSec'], 60)   # out of [0,86400] -> default

    def test_honors_valid(self):
        p = lib.retry_policy({'retryPolicy': {'maxAttempts': 3, 'backoffSec': 30, 'backoffMultiplier': 3}})
        self.assertEqual(p['maxAttempts'], 3)
        self.assertEqual(p['backoffSec'], 30)
        self.assertEqual(p['backoffMultiplier'], 3)


class RetryDelayTest(unittest.TestCase):
    def test_exponential_backoff(self):
        policy = {'backoffSec': 60, 'backoffMultiplier': 2, 'maxBackoffSec': 3600}
        self.assertEqual(lib.retry_delay(policy, 1), 60)    # 60 * 2^0
        self.assertEqual(lib.retry_delay(policy, 2), 120)   # 60 * 2^1
        self.assertEqual(lib.retry_delay(policy, 3), 240)   # 60 * 2^2

    def test_capped_at_max(self):
        policy = {'backoffSec': 1000, 'backoffMultiplier': 10, 'maxBackoffSec': 3600}
        self.assertEqual(lib.retry_delay(policy, 5), 3600)


class ApplyRetryTransitionTest(unittest.TestCase):
    def test_success_clears_state(self):
        task = {'retryState': {'attempt': 2}}
        res = lib.apply_retry_transition(task, '2026-06-28T00:00:00Z', 2, 'run1', 'success', _dt(2026, 6, 28, 0, 0))
        self.assertTrue(res['cleared'])
        self.assertNotIn('retryState', task)

    def test_failure_schedules_retry(self):
        task = {'retryPolicy': {'maxAttempts': 3, 'backoffSec': 60, 'backoffMultiplier': 2}}
        res = lib.apply_retry_transition(task, '2026-06-28T00:00:00Z', 1, 'run1', 'failure', _dt(2026, 6, 28, 0, 0))
        self.assertFalse(res['exhausted'])
        self.assertEqual(task['retryState']['retryEligibleAt'], '2026-06-28T00:01:00Z')  # +60s

    def test_failure_exhausts_at_max(self):
        task = {'retryPolicy': {'maxAttempts': 2}}
        res = lib.apply_retry_transition(task, '2026-06-28T00:00:00Z', 2, 'run1', 'failure', _dt(2026, 6, 28, 0, 0))
        self.assertTrue(res['exhausted'])
        self.assertIsNone(task['retryState']['retryEligibleAt'])
        self.assertEqual(task['retryState']['lastStatus'], 'exhausted')


class RetryViewTest(unittest.TestCase):
    def test_no_state_returns_none(self):
        self.assertIsNone(lib.retry_view({}, _dt(2026, 6, 28, 0, 0)))

    def test_ready_when_eligible_passed(self):
        task = {
            'retryPolicy': {'maxAttempts': 3},
            'retryState': {'attempt': 1, 'retryEligibleAt': '2026-06-28T00:00:00Z'},
        }
        view = lib.retry_view(task, _dt(2026, 6, 28, 1, 0))
        self.assertTrue(view['ready'])
        self.assertFalse(view['waiting'])

    def test_waiting_when_eligible_future(self):
        task = {
            'retryPolicy': {'maxAttempts': 3},
            'retryState': {'attempt': 1, 'retryEligibleAt': '2026-06-28T02:00:00Z'},
        }
        view = lib.retry_view(task, _dt(2026, 6, 28, 1, 0))
        self.assertTrue(view['waiting'])
        self.assertFalse(view['ready'])


class ScheduleKindsTest(unittest.TestCase):
    def test_interval_parse(self):
        spec = lib.parse_schedule('every 30m')
        self.assertEqual(spec['kind'], 'interval')
        self.assertEqual(spec['seconds'], 1800)
        self.assertEqual(lib.parse_schedule('every 2h')['seconds'], 7200)
        self.assertEqual(lib.parse_schedule('every 1d')['seconds'], 86400)

    def test_interval_bounds(self):
        with self.assertRaises(ValueError):
            lib.parse_schedule('every 400d')
        with self.assertRaises(ValueError):
            lib.parse_schedule('every 0m')

    def test_once_parse_forms(self):
        spec = lib.parse_schedule('at 2026-08-01T09:00:00Z')
        self.assertEqual(spec['kind'], 'once')
        self.assertEqual(spec['runAt'], _dt(2026, 8, 1, 9, 0))
        bare = lib.parse_schedule('2026-08-01T09:00:00Z')
        self.assertEqual(bare['kind'], 'once')
        self.assertEqual(bare['runAt'], _dt(2026, 8, 1, 9, 0))

    def test_once_naive_anchored_to_task_timezone(self):
        spec = lib.parse_schedule('at 2026-08-01T09:00', 'Asia/Seoul')
        self.assertEqual(spec['runAt'], _dt(2026, 8, 1, 0, 0))  # KST-9

    def test_unknown_timezone_fails_closed(self):
        with self.assertRaises(ValueError):
            lib.parse_schedule('@daily', 'Mars/OlympusMons')

    def test_cron_kind_marked(self):
        self.assertEqual(lib.parse_schedule('@daily')['kind'], 'cron')


class TimezoneCronTest(unittest.TestCase):
    def test_kst_daily_9am_matches_midnight_utc(self):
        spec = lib.parse_schedule('0 9 * * *', 'Asia/Seoul')
        occ, truncated = lib.schedule_occurrences(
            spec, _dt(2026, 8, 1, 0, 0), _dt(2026, 8, 2, 0, 0))
        self.assertFalse(truncated)
        self.assertEqual(occ, [_dt(2026, 8, 2, 0, 0)])  # 09:00 KST == 00:00 UTC

    def test_next_after_in_task_timezone(self):
        spec = lib.parse_schedule('0 9 * * *', 'Asia/Seoul')
        self.assertEqual(lib.next_after(spec, _dt(2026, 8, 1, 1, 0)),
                         _dt(2026, 8, 2, 0, 0))


class IntervalOccurrenceTest(unittest.TestCase):
    def test_never_run_is_due_once_now(self):
        spec = lib.parse_schedule('every 30m')
        occ, truncated = lib.schedule_occurrences(spec, None, _dt(2026, 8, 1, 12, 0))
        self.assertEqual(occ, [_dt(2026, 8, 1, 12, 0)])
        self.assertFalse(truncated)

    def test_free_running_from_last_run(self):
        spec = lib.parse_schedule('every 30m')
        occ, _ = lib.schedule_occurrences(
            spec, _dt(2026, 8, 1, 10, 0), _dt(2026, 8, 1, 11, 30))
        self.assertEqual(occ, [_dt(2026, 8, 1, 10, 30),
                               _dt(2026, 8, 1, 11, 0),
                               _dt(2026, 8, 1, 11, 30)])

    def test_anchor_keeps_phase(self):
        spec = lib.parse_schedule('every 1h')
        anchor = _dt(2026, 8, 1, 0, 15)
        occ, _ = lib.schedule_occurrences(
            spec, _dt(2026, 8, 1, 1, 15), _dt(2026, 8, 1, 3, 20), anchor=anchor)
        self.assertEqual(occ, [_dt(2026, 8, 1, 2, 15), _dt(2026, 8, 1, 3, 15)])

    def test_next_after_phase_aligned(self):
        spec = lib.parse_schedule('every 1h')
        anchor = _dt(2026, 8, 1, 0, 15)
        self.assertEqual(lib.next_after(spec, _dt(2026, 8, 1, 2, 30), anchor=anchor),
                         _dt(2026, 8, 1, 3, 15))


class OnceOccurrenceTest(unittest.TestCase):
    def test_due_when_reached_and_not_run(self):
        spec = lib.parse_schedule('at 2026-08-01T09:00:00Z')
        occ, _ = lib.schedule_occurrences(spec, None, _dt(2026, 8, 1, 9, 5))
        self.assertEqual(occ, [_dt(2026, 8, 1, 9, 0)])

    def test_not_due_after_it_already_ran(self):
        spec = lib.parse_schedule('at 2026-08-01T09:00:00Z')
        occ, _ = lib.schedule_occurrences(
            spec, _dt(2026, 8, 1, 9, 0), _dt(2026, 8, 1, 10, 0))
        self.assertEqual(occ, [])

    def test_not_due_before_run_at(self):
        spec = lib.parse_schedule('at 2026-08-01T09:00:00Z')
        occ, _ = lib.schedule_occurrences(spec, None, _dt(2026, 8, 1, 8, 59))
        self.assertEqual(occ, [])
        self.assertEqual(lib.next_after(spec, _dt(2026, 8, 1, 8, 59)),
                         _dt(2026, 8, 1, 9, 0))
        self.assertIsNone(lib.next_after(spec, _dt(2026, 8, 1, 9, 0)))


class FmtDtTest(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(lib.fmt_dt(None))

    def test_z_suffix(self):
        self.assertEqual(lib.fmt_dt(_dt(2026, 6, 28, 10, 30)), '2026-06-28T10:30:00Z')


if __name__ == '__main__':
    unittest.main()
