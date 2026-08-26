#!/usr/bin/env python3
"""Direct unit tests for agent_cron_lib (pure schedule + retry helpers).

Run standalone: python3 scripts/agent_cron_lib_test.py
These exercise the deterministic cron/retry math that previously lived inline in
agent_cron.py and had only indirect CLI-level coverage via agent-cron.test.sh.
"""

import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone

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


class SuccessExitCodesTest(unittest.TestCase):
    def test_default_is_zero(self):
        self.assertEqual(lib.success_exit_codes({}), {0})
        self.assertEqual(lib.success_exit_codes({'successExitCodes': None}), {0})
        self.assertEqual(lib.success_exit_codes({'successExitCodes': []}), {0})

    def test_honors_declared_codes(self):
        self.assertEqual(lib.success_exit_codes({'successExitCodes': [0, 1]}), {0, 1})

    def test_ignores_invalid_entries(self):
        self.assertEqual(lib.success_exit_codes({'successExitCodes': [0, 1, 300, -1, 'x']}), {0, 1})

    def test_all_invalid_falls_back_to_zero(self):
        self.assertEqual(lib.success_exit_codes({'successExitCodes': [300, -1]}), {0})


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

    def test_no_policy_failure_does_not_record_retry_state(self):
        # Per #911: a task that declared no retryPolicy has no retry concept,
        # so failure must never be labelled retry-exhausted.
        task = {}
        res = lib.apply_retry_transition(task, '2026-06-28T00:00:00Z', 1, 'run1', 'failed', _dt(2026, 6, 28, 0, 0))
        self.assertFalse(res['exhausted'])
        self.assertNotIn('retryState', task)

    def test_no_policy_failure_clears_stale_retry_state(self):
        task = {'retryState': {'attempt': 5, 'lastStatus': 'exhausted'}}
        res = lib.apply_retry_transition(task, '2026-06-28T00:00:00Z', 1, 'run1', 'failed', _dt(2026, 6, 28, 0, 0))
        self.assertFalse(res['exhausted'])
        self.assertNotIn('retryState', task)

    def test_explicit_policy_still_records_retry_state(self):
        # A task that explicitly declared retryPolicy (even maxAttempts: 1) opted
        # into the retry framework and exhaustion remains valid.
        task = {'retryPolicy': {'maxAttempts': 1}}
        res = lib.apply_retry_transition(task, '2026-06-28T00:00:00Z', 1, 'run1', 'failed', _dt(2026, 6, 28, 0, 0))
        self.assertTrue(res['exhausted'])
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


def _slow_next_occurrence(spec, after, max_minutes=366 * 24 * 60):
    """Reference minute walk: the pre-fast-path next_occurrence implementation."""
    cur = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(max_minutes):
        if lib.cron_matches(lib._local(cur, spec), spec):
            return cur
        cur += timedelta(minutes=1)
    return None


def _slow_iter_occurrences(spec, start_exclusive, end_inclusive, cap=lib.OCCURRENCE_SCAN_LIMIT):
    """Reference minute walk: the pre-fast-path iter_occurrences implementation."""
    cur = (start_exclusive + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = end_inclusive.replace(second=0, microsecond=0)
    out = []
    truncated = False
    while cur <= end:
        if lib.cron_matches(lib._local(cur, spec), spec):
            if len(out) >= cap:
                truncated = True
                break
            out.append(cur)
        cur += timedelta(minutes=1)
    return out, truncated


JUMP_SPECS = (
    '* * * * *',
    '*/5 * * * *',
    '0 * * * *',
    '30 9 * * *',
    '0,30 8,20 * * 1,3,5',
    '0 0 * * 0',
    '15 14 1 * *',
    '59 23 31 12 *',
    '0 0 13 * 5',        # DOM/DOW union rule: 13th of the month OR Friday
    '30 2 * * *',        # sits inside the America/New_York spring-forward gap
    '30 1 * * *',        # repeated during the America/New_York fall-back hour
    '0 0 1 1 *',
)
JUMP_TZS = ('UTC', 'Asia/Seoul', 'America/New_York')


class FieldJumpEquivalenceTest(unittest.TestCase):
    """The field-jump fast path must return exactly what the minute walk did."""

    def assert_next_equal(self, expr, tz, after, max_minutes):
        spec = lib.parse_schedule(expr, tz)
        want = _slow_next_occurrence(spec, after, max_minutes)
        got = lib.next_occurrence(spec, after, max_minutes)
        self.assertEqual(want, got, f'{expr} tz={tz} after={after} max={max_minutes}')

    def assert_iter_equal(self, expr, tz, start, end, cap=lib.OCCURRENCE_SCAN_LIMIT):
        spec = lib.parse_schedule(expr, tz)
        want = _slow_iter_occurrences(spec, start, end, cap)
        got = lib.iter_occurrences(spec, start, end, cap)
        self.assertEqual(want, got, f'{expr} tz={tz} start={start} end={end} cap={cap}')

    def test_matrix_over_calendar_edges(self):
        afters = [
            _dt(2026, 1, 1, 0, 0),        # year start
            _dt(2026, 2, 28, 23, 59),     # non-leap February end
            _dt(2028, 2, 28, 12, 0),      # leap-year February
            _dt(2026, 12, 31, 23, 30),    # year end
            _dt(2026, 6, 30, 23, 59),     # month boundary
            _dt(2026, 8, 1, 14, 59),      # plain mid-year point
        ]
        for expr in JUMP_SPECS:
            for tz in JUMP_TZS:
                for after in afters:
                    self.assert_next_equal(expr, tz, after, 2 * 1440)

    def test_matrix_around_new_york_dst_transitions(self):
        # 2026: spring forward Mar 8 02:00 EST (07:00Z), fall back Nov 1
        # 02:00 EDT (06:00Z). Probe minutes before, at, and after both.
        afters = [
            _dt(2026, 3, 8, 6, 15), _dt(2026, 3, 8, 6, 59),
            _dt(2026, 3, 8, 7, 0), _dt(2026, 3, 8, 7, 1),
            _dt(2026, 11, 1, 4, 30), _dt(2026, 11, 1, 5, 15),
            _dt(2026, 11, 1, 5, 59), _dt(2026, 11, 1, 6, 0),
            _dt(2026, 11, 1, 6, 30),
        ]
        for expr in JUMP_SPECS:
            for after in afters:
                self.assert_next_equal(expr, 'America/New_York', after, 2 * 1440)

    def test_sparse_specs_over_long_horizons(self):
        for expr in ('0 0 1 1 *', '59 23 31 12 *', '15 14 1 * *'):
            for tz in JUMP_TZS:
                self.assert_next_equal(expr, tz, _dt(2026, 12, 30, 3, 7), 5 * 1440)

    def test_no_match_within_horizon_is_none_for_both(self):
        for tz in JUMP_TZS:
            spec = lib.parse_schedule('0 0 1 1 *', tz)
            after = _dt(2026, 1, 2, 0, 0)
            self.assertIsNone(_slow_next_occurrence(spec, after, 1440))
            self.assertIsNone(lib.next_occurrence(spec, after, 1440))

    def test_horizon_endpoints_match_the_walk(self):
        # The walk checked exactly max_minutes candidates starting at after+1m;
        # a match on the last candidate is found, one candidate later is not.
        spec = lib.parse_schedule('0 * * * *')
        self.assertEqual(lib.next_occurrence(spec, _dt(2026, 6, 28, 9, 0), 60),
                         _dt(2026, 6, 28, 10, 0))
        self.assertIsNone(lib.next_occurrence(spec, _dt(2026, 6, 28, 9, 0), 59))

    def test_iter_windows_and_caps(self):
        windows = [
            (_dt(2026, 11, 1, 0, 0), _dt(2026, 11, 2, 0, 0)),   # NY fall-back day
            (_dt(2026, 3, 8, 0, 0), _dt(2026, 3, 9, 12, 0)),    # NY spring-forward day
            (_dt(2026, 7, 31, 12, 0), _dt(2026, 8, 2, 12, 0)),  # month boundary
        ]
        for expr in ('*/5 * * * *', '0 * * * *', '30 1 * * *', '30 2 * * *', '30 9 * * *'):
            for tz in JUMP_TZS:
                for start, end in windows:
                    self.assert_iter_equal(expr, tz, start, end)
                    self.assert_iter_equal(expr, tz, start, end, cap=3)

    def test_random_probes_match_the_walk(self):
        rng = random.Random(20260826)
        base = _dt(2025, 1, 1, 0, 0)
        for _ in range(40):
            expr = rng.choice(JUMP_SPECS)
            tz = rng.choice(JUMP_TZS)
            after = base + timedelta(minutes=rng.randrange(4 * 366 * 1440))
            self.assert_next_equal(expr, tz, after, 1440)


class DstBehaviorTest(unittest.TestCase):
    """Pinned expectations at the 2026 America/New_York transitions."""

    def test_fall_back_runs_both_passes_of_the_repeated_hour(self):
        spec = lib.parse_schedule('30 1 * * *', 'America/New_York')
        out, truncated = lib.iter_occurrences(
            spec, _dt(2026, 11, 1, 0, 0), _dt(2026, 11, 1, 12, 0))
        self.assertFalse(truncated)
        self.assertEqual(out, [_dt(2026, 11, 1, 5, 30),   # 01:30 EDT
                               _dt(2026, 11, 1, 6, 30)])  # 01:30 EST

    def test_spring_forward_skips_the_nonexistent_local_time(self):
        spec = lib.parse_schedule('30 2 * * *', 'America/New_York')
        # 2026-03-08 02:30 never exists locally; the next match is Mar 9 02:30 EDT.
        self.assertEqual(lib.next_occurrence(spec, _dt(2026, 3, 8, 0, 0)),
                         _dt(2026, 3, 9, 6, 30))


class FmtDtTest(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(lib.fmt_dt(None))

    def test_z_suffix(self):
        self.assertEqual(lib.fmt_dt(_dt(2026, 6, 28, 10, 30)), '2026-06-28T10:30:00Z')


if __name__ == '__main__':
    unittest.main()
