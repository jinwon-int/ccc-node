#!/usr/bin/env python3
"""Change-based fleet alert state (#2086); hermetic, no fleet access."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import fleet_watch_state as fws

SCRIPT = Path(__file__).resolve().parent / 'fleet_watch_state.py'

OK_A = 'OK node-a (/opt/ccc-node)\nOK node-a channel=matrix reason=available\n'
DEG_A = ('DEGRADED node-a runtime=/opt/ccc-node reason=health-stale\n'
         'OK node-a channel=matrix reason=available\n')


class StateRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state' / 'fleet-watch.json'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('CCC_FLEET_WATCH_')}

    def run_watch(self, text, now, raw_exit=0, **env):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--state-file', str(self.state),
             '--raw-exit', str(raw_exit), '--now', now],
            input=text, text=True, capture_output=True, env={**self.env, **env},
        )
        return proc.returncode, proc.stdout.splitlines(), proc.stderr

    def entries(self):
        return json.loads(self.state.read_text())['entries']

    # --- confirmation -------------------------------------------------------

    def test_new_abnormal_pages_only_after_confirmation(self):
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], 'PENDING DEGRADED node-a channel=telegram '
                                 'runtime=/opt/ccc-node reason=health-stale confirm=1/2')
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T00:15:00Z')
        self.assertEqual(rc, 1)
        self.assertEqual(out[0], 'NEW DEGRADED node-a channel=telegram runtime=/opt/ccc-node '
                                 'reason=health-stale since=2026-10-01T00:00:00Z')
        self.assertFalse(any(line.startswith('OK ') for line in out), out)
        self.assertTrue(out[-1].startswith('SUMMARY checked=2 ok=1 new=1 '), out[-1])
        self.assertTrue(out[-1].endswith(' page=yes'))
        # Already alerted: quiet until the re-alert interval.
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 0)
        self.assertTrue(out[0].startswith('KNOWN DEGRADED node-a channel=telegram '), out)
        self.assertIn('next-alert=2026-10-01T06:15:00Z', out[0])

    def test_unverified_needs_more_runs(self):
        text = 'UNVERIFIED node-a channel=matrix reason=health-pid\n'
        for minute in ('00', '15'):
            rc, out, _ = self.run_watch(text, f'2026-10-01T00:{minute}:00Z')
            self.assertEqual(rc, 0, out)
        self.assertIn('confirm=2/3', out[0])
        rc, out, _ = self.run_watch(text, '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 1)
        self.assertTrue(out[0].startswith('NEW UNVERIFIED node-a channel=matrix reason=health-pid'))

    def test_thresholds_are_configurable(self):
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T00:00:00Z', CCC_FLEET_WATCH_CONFIRM='1')
        self.assertEqual(rc, 1)
        self.assertTrue(out[0].startswith('NEW DEGRADED node-a'))

    def test_one_run_blip_resets_the_streak(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        rc, out, _ = self.run_watch(OK_A, '2026-10-01T00:15:00Z')
        self.assertEqual(rc, 0)
        self.assertEqual(out, ['SUMMARY checked=2 ok=2 new=0 still=0 recovered=0 pending=0 '
                               'known=0 recovering=0 page=no'])
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 0)
        self.assertIn('confirm=1/2', out[0])

    # --- chronic issues ------------------------------------------------------

    def test_known_chronic_issue_does_not_mask_a_new_one(self):
        chronic = 'DEGRADED node-b channel=matrix reason=health-degraded\n'
        self.run_watch(OK_A + chronic, '2026-10-01T00:00:00Z')
        rc, _, _ = self.run_watch(OK_A + chronic, '2026-10-01T00:15:00Z')
        self.assertEqual(rc, 1)
        self.run_watch(DEG_A + chronic, '2026-10-01T00:30:00Z')
        rc, out, _ = self.run_watch(DEG_A + chronic, '2026-10-01T00:45:00Z')
        self.assertEqual(rc, 1)
        self.assertTrue(out[0].startswith('NEW DEGRADED node-a channel=telegram'), out)
        self.assertTrue(out[1].startswith('KNOWN DEGRADED node-b channel=matrix'), out)
        self.assertNotIn('NEW DEGRADED node-b', '\n'.join(out))

    def test_realert_is_staged(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z')
        rc, _, _ = self.run_watch(DEG_A, '2026-10-01T06:00:00Z')
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T06:15:00Z')
        self.assertEqual(rc, 1)
        self.assertEqual(out[0], 'STILL DEGRADED node-a channel=telegram runtime=/opt/ccc-node '
                                 'reason=health-stale since=2026-10-01T00:00:00Z for=6h15m '
                                 'alerts=2')
        # The second interval (24h) applies from the second alert on.
        rc, out, _ = self.run_watch(DEG_A, '2026-10-02T00:15:00Z')
        self.assertEqual(rc, 0)
        self.assertIn('next-alert=2026-10-02T06:15:00Z', out[0])
        rc, out, _ = self.run_watch(DEG_A, '2026-10-02T06:15:00Z')
        self.assertEqual(rc, 1)
        self.assertIn('alerts=3', out[0])

    def test_realert_can_be_disabled(self):
        env = {'CCC_FLEET_WATCH_REALERT': 'off'}
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z', **env)
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z', **env)
        rc, out, _ = self.run_watch(DEG_A, '2026-10-05T00:00:00Z', **env)
        self.assertEqual(rc, 0)
        self.assertIn('next-alert=off', out[0])

    def test_worse_verdict_on_an_alerted_pair_is_new(self):
        down = 'DOWN node-a\n'
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z')
        rc, out, _ = self.run_watch(down, '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 0)
        self.assertTrue(out[0].startswith('PENDING DOWN node-a'), out)
        rc, out, _ = self.run_watch(down, '2026-10-01T00:45:00Z')
        self.assertEqual(rc, 1)
        # `since` is when the pair first left OK, not when DOWN began.
        self.assertEqual(out[0], 'NEW DOWN node-a channel=telegram since=2026-10-01T00:00:00Z')

    # --- recovery ------------------------------------------------------------

    def test_recovery_of_an_alerted_pair_is_confirmed_then_reported(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z')
        rc, out, _ = self.run_watch(OK_A, '2026-10-01T10:00:00Z')
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], 'RECOVERING node-a channel=telegram was=DEGRADED confirm=1/2')
        rc, out, _ = self.run_watch(OK_A, '2026-10-01T10:15:00Z')
        self.assertEqual(rc, 1)
        self.assertEqual(out[0], 'RECOVERED node-a channel=telegram was=DEGRADED '
                                 'since=2026-10-01T00:00:00Z for=10h15m')
        rc, out, _ = self.run_watch(OK_A, '2026-10-01T10:30:00Z')
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 1, out)
        self.assertIsNone(self.entries()['node-a/telegram']['alerted'])

    def test_a_single_ok_run_does_not_reopen_a_flapping_alert(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z')
        self.run_watch(OK_A, '2026-10-01T00:30:00Z')
        rc, out, _ = self.run_watch(DEG_A, '2026-10-01T00:45:00Z')
        self.assertEqual(rc, 0)
        self.assertTrue(out[0].startswith('KNOWN DEGRADED node-a'), out)

    def test_recovery_paging_is_optional(self):
        env = {'CCC_FLEET_WATCH_PAGE_RECOVERY': '0'}
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z', **env)
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z', **env)
        self.run_watch(OK_A, '2026-10-01T00:30:00Z', **env)
        rc, out, _ = self.run_watch(OK_A, '2026-10-01T00:45:00Z', **env)
        self.assertEqual(rc, 0)
        self.assertTrue(out[0].startswith('RECOVERED node-a'), out)

    def test_unseen_pairs_are_left_alone_and_eventually_pruned(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z')
        # A run that only saw another node must not recover or forget node-a.
        rc, out, _ = self.run_watch('OK node-b (/opt/ccc-node)\n', '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 0)
        self.assertEqual(self.entries()['node-a/telegram']['alerted'], 'DEGRADED')
        self.run_watch('OK node-b (/opt/ccc-node)\n', '2026-10-09T00:00:00Z')
        self.assertNotIn('node-a/telegram', self.entries())

    def test_no_verdicts_with_a_failed_watch_is_itself_a_finding(self):
        for minute in ('00', '15'):
            self.run_watch('', f'2026-10-01T00:{minute}:00Z', raw_exit=2)
        rc, out, _ = self.run_watch('', '2026-10-01T00:30:00Z', raw_exit=2)
        self.assertEqual(rc, 1)
        self.assertEqual(out[0], 'NEW UNVERIFIED watcher channel=telegram '
                                 'inspection=no-verdicts raw-exit=2 since=2026-10-01T00:00:00Z')

    # --- the state file ------------------------------------------------------

    def test_state_file_is_owner_only(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        lock = self.state.with_name(self.state.name + '.lock')
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)
        doc = json.loads(self.state.read_text())
        self.assertEqual(doc['schema'], fws.SCHEMA)
        self.assertEqual(sorted(doc['entries']), ['node-a/matrix', 'node-a/telegram'])

    def _alerted(self):
        self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.run_watch(DEG_A, '2026-10-01T00:15:00Z')

    def assert_reset(self, warning):
        rc, out, err = self.run_watch(DEG_A, '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 0, err)
        self.assertIn(warning, err)
        self.assertIn('confirm=1/2', out[0])  # empty state: back to pending
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.state.read_text())['schema'], fws.SCHEMA)

    def test_corrupt_json_resets_with_a_warning(self):
        self._alerted()
        self.state.write_text('{"schema": "ccc.fleet-watch-state.v1", "entries": {')
        self.assert_reset('not valid JSON')

    def test_binary_garbage_resets_with_a_warning(self):
        self._alerted()
        self.state.write_bytes(b'\xff\xfe\x00garbage')
        self.assert_reset('not valid JSON')

    def test_unknown_schema_resets_with_a_warning(self):
        self._alerted()
        self.state.write_text(json.dumps({'schema': 'other', 'entries': {}}))
        self.assert_reset('unknown schema')
        self.state.write_text('[]')
        self.assert_reset('unknown schema')

    def test_malformed_entries_are_dropped_individually(self):
        self._alerted()
        doc = json.loads(self.state.read_text())
        doc['entries']['bad key'] = {'verdict': 'OK'}
        doc['entries']['node-a/matrix']['streak'] = 'many'
        doc['entries']['node-z/telegram'] = dict(doc['entries']['node-a/telegram'],
                                                 lastAlertAt=None)
        self.state.write_text(json.dumps(doc))
        rc, out, err = self.run_watch(DEG_A, '2026-10-01T00:30:00Z')
        self.assertEqual(rc, 0)
        self.assertIn('dropped 3 malformed state entries', err)
        self.assertTrue(out[0].startswith('KNOWN DEGRADED node-a'), out)

    def test_group_readable_state_is_rejected_and_rewritten(self):
        self._alerted()
        self.state.chmod(0o644)
        self.assert_reset('state file rejected')

    def test_symlinked_state_is_not_followed(self):
        self._alerted()
        target = Path(self.tmp.name) / 'elsewhere.json'
        target.write_text(self.state.read_text())
        target.chmod(0o600)
        before = target.read_text()
        self.state.unlink()
        self.state.symlink_to(target)
        self.assert_reset('state file rejected')
        self.assertFalse(self.state.is_symlink())
        self.assertEqual(target.read_text(), before)

    def test_unwritable_state_location_exits_two(self):
        blocker = Path(self.tmp.name) / 'file'
        blocker.write_text('x')
        self.state = blocker / 'state.json'
        rc, out, err = self.run_watch(DEG_A, '2026-10-01T00:00:00Z')
        self.assertEqual(rc, 2)
        self.assertEqual(out, [])
        self.assertIn('could not be updated', err)


class Parsing(unittest.TestCase):
    def test_channels_and_details(self):
        rows = fws.parse_verdicts(
            'OK node-a (/opt/ccc-node, recovered-from:health-stale)\n'
            'DOWN node-b channel=matrix reason=no-process\n'
            'not a verdict\n'
            ' DOWN leading-space\n'
            'DOWNSTREAM node-c\n'
            'UNREACHABLE node-c\n')
        self.assertEqual(list(rows), ['node-a/telegram', 'node-b/matrix', 'node-c/telegram'])
        self.assertEqual(rows['node-b/matrix'], ('DOWN', 'node-b', 'matrix', 'reason=no-process'))
        self.assertEqual(rows['node-c/telegram'], ('UNREACHABLE', 'node-c', 'telegram', ''))

    def test_abnormal_row_wins_over_ok_for_the_same_pair(self):
        rows = fws.parse_verdicts('OK node-a x\nDEGRADED node-a reason=y\nOK node-a z\n')
        self.assertEqual(rows['node-a/telegram'][0], 'DEGRADED')

    def test_details_are_bounded(self):
        rows = fws.parse_verdicts('DRIFT node-a ' + 'x' * 500 + '\n')
        self.assertEqual(len(rows['node-a/telegram'][3]), fws.DETAIL_MAX)

    def test_realert_parsing(self):
        hour = fws.timedelta(hours=1)
        self.assertEqual(fws.parse_realert('1h,30m'), [hour, hour / 2])
        self.assertEqual(fws.parse_realert('off'), [])
        self.assertEqual(fws.parse_realert('soon'), [6 * hour, 24 * hour])


if __name__ == '__main__':
    unittest.main()
