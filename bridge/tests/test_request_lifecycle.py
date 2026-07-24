"""Deterministic tests for the provider-neutral request lifecycle (#346)."""

import unittest

from telegram_bot.core.request_lifecycle import (
    RequestLifecycle,
    RequestPhase,
    TerminalAttemptKind,
)


TERMINALS = (
    RequestPhase.COMPLETED,
    RequestPhase.FAILED,
    RequestPhase.CANCELED,
    RequestPhase.TIMEOUT,
    RequestPhase.INTERRUPTED,
)


class RequestLifecycleTests(unittest.TestCase):
    def test_normal_admission_approval_and_completion(self):
        lifecycle = RequestLifecycle()
        self.assertTrue(lifecycle.is_waiting_for_turn)
        self.assertTrue(lifecycle.admit())
        self.assertEqual(lifecycle.phase, RequestPhase.WORKING)

        lease = lifecycle.begin_approval()
        self.assertIsNotNone(lease)
        self.assertTrue(lifecycle.is_input_required)
        self.assertIsNone(lifecycle.begin_approval())
        assert lease is not None
        self.assertTrue(lifecycle.end_approval(lease))
        self.assertEqual(lifecycle.phase, RequestPhase.WORKING)

        attempt = lifecycle.try_terminal(RequestPhase.COMPLETED, cause="normal-completion")
        self.assertEqual(attempt.kind, TerminalAttemptKind.WON)
        self.assertEqual(lifecycle.terminal_outcome, RequestPhase.COMPLETED)
        self.assertEqual(lifecycle.terminal_cause, "normal-completion")

    def test_terminal_first_wins_in_both_orderings(self):
        for first in TERMINALS:
            for second in TERMINALS:
                with self.subTest(first=first, second=second):
                    lifecycle = RequestLifecycle()
                    won = lifecycle.try_terminal(first, cause=f"first-{first.value}")
                    lost = lifecycle.try_terminal(second, cause=f"second-{second.value}")
                    self.assertEqual(won.kind, TerminalAttemptKind.WON)
                    expected = (
                        TerminalAttemptKind.ALREADY_SAME
                        if first is second
                        else TerminalAttemptKind.LOST
                    )
                    self.assertEqual(lost.kind, expected)
                    self.assertEqual(lost.phase, first)
                    self.assertEqual(lifecycle.terminal_outcome, first)
                    self.assertEqual(lifecycle.terminal_cause, f"first-{first.value}")

    def test_stale_approval_lease_cannot_resume_terminal_or_new_approval(self):
        lifecycle = RequestLifecycle()
        self.assertTrue(lifecycle.admit())
        first = lifecycle.begin_approval()
        assert first is not None
        self.assertTrue(lifecycle.end_approval(first))

        second = lifecycle.begin_approval()
        assert second is not None
        self.assertFalse(lifecycle.end_approval(first))
        self.assertTrue(lifecycle.is_input_required)
        lifecycle.try_terminal(RequestPhase.CANCELED, cause="request-canceled")
        self.assertFalse(lifecycle.end_approval(second))
        self.assertEqual(lifecycle.phase, RequestPhase.CANCELED)

    def test_terminal_invalidates_approval_and_blocks_admission(self):
        lifecycle = RequestLifecycle()
        lifecycle.try_terminal(RequestPhase.TIMEOUT, cause="admission-timeout")
        self.assertFalse(lifecycle.admit())
        self.assertIsNone(lifecycle.begin_approval())
        self.assertEqual(lifecycle.phase, RequestPhase.TIMEOUT)

    def test_finalization_has_one_terminal_winner(self):
        lifecycle = RequestLifecycle()
        self.assertFalse(lifecycle.begin_finalization())
        lifecycle.try_terminal(RequestPhase.FAILED, cause="runtime-error")
        self.assertTrue(lifecycle.begin_finalization())
        self.assertFalse(lifecycle.begin_finalization())

    def test_nonterminal_target_and_blank_cause_are_programming_errors(self):
        lifecycle = RequestLifecycle()
        with self.assertRaises(ValueError):
            lifecycle.try_terminal(RequestPhase.WORKING, cause="invalid")
        with self.assertRaises(ValueError):
            lifecycle.try_terminal(RequestPhase.FAILED, cause="")


if __name__ == "__main__":
    unittest.main()
