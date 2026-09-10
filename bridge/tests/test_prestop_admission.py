"""Source-only admission invariants; no service, provider or network calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from telegram_bot.core.prestop_admission import AdmissionError, PrestopAdmission


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


@pytest.fixture
def gate():
    clock = Clock()
    return PrestopAdmission("process-start-A", "generation-A", clock=clock), clock


def test_work_held_through_final_delivery_and_commit_once(gate):
    admission, _ = gate
    token = admission.admit()
    evidence = admission.close(timeout=10)
    assert not admission.ready(evidence)
    with pytest.raises(AdmissionError):
        admission.admit()
    with pytest.raises(AdmissionError):
        admission.commit(evidence)
    # Retryable delivery errors leave the token outstanding.
    assert not admission.ready(evidence)
    admission.finish(token, delivered=True)
    assert admission.ready(evidence)
    admission.commit(evidence)
    for operation in (admission.ready, admission.commit, admission.cancel):
        with pytest.raises(AdmissionError):
            operation(evidence)
    with pytest.raises(AdmissionError):
        admission.admit()


def test_duplicate_and_unknown_completion_do_not_release_other_work(gate):
    admission, _ = gate
    first, second = admission.admit(), admission.admit()
    evidence = admission.close(timeout=10)
    admission.finish(first, delivered=True)
    for token in (first, "foreign"):
        with pytest.raises(AdmissionError):
            admission.finish(token, delivered=True)
    assert not admission.ready(evidence)
    admission.finish(second, delivered=True)
    admission.commit(evidence)


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt", "foreign"),
        ("process_identity", "other-start"),
        ("generation", "other-generation"),
        ("deadline", 999.0),
    ],
)
def test_foreign_evidence_cannot_observe_commit_or_cancel(gate, field, value):
    admission, _ = gate
    evidence = admission.close(timeout=10)
    foreign = replace(evidence, **{field: value})
    for operation in (admission.ready, admission.commit, admission.cancel):
        with pytest.raises(AdmissionError):
            operation(foreign)
    assert admission.ready(evidence)


def test_cancel_retains_work_and_invalidates_previous_attempt(gate):
    admission, _ = gate
    token = admission.admit()
    old = admission.close(timeout=10)
    admission.cancel(old)
    additional = admission.admit()
    current = admission.close(timeout=10)
    assert old != current
    for operation in (admission.ready, admission.commit, admission.cancel):
        with pytest.raises(AdmissionError):
            operation(old)
    admission.finish(token, delivered=True)
    assert not admission.ready(current)
    admission.finish(additional, delivered=True)
    admission.commit(current)


@pytest.mark.parametrize("during_drain", [False, True])
def test_terminal_delivery_failure_latches_closed(gate, during_drain):
    admission, _ = gate
    token = admission.admit()
    evidence = admission.close(timeout=10) if during_drain else None
    admission.finish(token, delivered=False)
    with pytest.raises(AdmissionError):
        admission.admit()
    with pytest.raises(AdmissionError):
        admission.close(timeout=10)
    if evidence:
        for operation in (admission.ready, admission.commit, admission.cancel):
            with pytest.raises(AdmissionError):
                operation(evidence)


@pytest.mark.parametrize("value", [110.0, 111.0, 99.0, float("nan"), float("inf")])
def test_deadline_and_invalid_clock_fail_closed(gate, value):
    admission, clock = gate
    evidence = admission.close(timeout=10)
    assert admission.ready(evidence)
    clock.value = value
    with pytest.raises(AdmissionError):
        admission.commit(evidence)
    clock.value = 101.0
    for operation in (admission.ready, admission.commit, admission.cancel):
        with pytest.raises(AdmissionError):
            operation(evidence)
    with pytest.raises(AdmissionError):
        admission.admit()


def test_clock_exception_latches_closed():
    def broken_clock():
        raise OSError("clock unavailable")

    admission = PrestopAdmission("start", "generation", clock=broken_clock)
    with pytest.raises(AdmissionError, match="clock unavailable"):
        admission.close(timeout=1)
    with pytest.raises(AdmissionError):
        admission.admit()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_does_not_close_admission(gate, timeout):
    admission, _ = gate
    with pytest.raises(ValueError):
        admission.close(timeout=timeout)
    admission.admit()


def test_duplicate_close_does_not_replace_owner(gate):
    admission, _ = gate
    evidence = admission.close(timeout=10)
    with pytest.raises(AdmissionError):
        admission.close(timeout=20)
    admission.commit(evidence)


def test_invalid_delivery_outcome_retains_work(gate):
    admission, _ = gate
    token = admission.admit()
    evidence = admission.close(timeout=10)
    with pytest.raises(ValueError):
        admission.finish(token, delivered=1)
    assert not admission.ready(evidence)


def test_foreign_gate_cannot_reuse_evidence(gate):
    admission, _ = gate
    other = PrestopAdmission("process-start-A", "generation-A", clock=Clock())
    foreign = other.close(timeout=10)
    own = admission.close(timeout=10)
    with pytest.raises(AdmissionError):
        admission.commit(foreign)
    admission.commit(own)


def test_concurrent_admission_and_close_are_serialized(gate):
    admission, _ = gate
    barrier = threading.Barrier(9)

    def admit():
        barrier.wait(timeout=5)
        try:
            return admission.admit()
        except AdmissionError:
            return None

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(admit) for _ in range(8)]
        barrier.wait(timeout=5)
        evidence = admission.close(timeout=10)
        tokens = [token for future in futures if (token := future.result(timeout=5))]
    assert admission.ready(evidence) == (not tokens)
    for token in tokens:
        admission.finish(token, delivered=True)
    admission.commit(evidence)


def test_concurrent_commits_have_exactly_one_winner(gate):
    admission, _ = gate
    evidence = admission.close(timeout=10)
    barrier = threading.Barrier(8)

    def commit():
        barrier.wait(timeout=5)
        try:
            admission.commit(evidence)
            return True
        except AdmissionError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: commit(), range(8))) == 1


def test_expired_cancel_cannot_reopen(gate):
    admission, clock = gate
    evidence = admission.close(timeout=10)
    clock.value = 110
    with pytest.raises(AdmissionError):
        admission.cancel(evidence)
    with pytest.raises(AdmissionError):
        admission.admit()


def test_clock_exception_during_commit_cannot_reopen():
    readings = iter([100.0])
    admission = PrestopAdmission("start", "generation", clock=lambda: next(readings))
    evidence = admission.close(timeout=10)
    with pytest.raises(AdmissionError):
        admission.commit(evidence)
    with pytest.raises(AdmissionError):
        admission.cancel(evidence)


def test_completion_after_failure_does_not_clear_failure(gate):
    admission, _ = gate
    first, second = admission.admit(), admission.admit()
    evidence = admission.close(timeout=10)
    admission.finish(first, delivered=False)
    admission.finish(second, delivered=True)
    with pytest.raises(AdmissionError):
        admission.commit(evidence)
    with pytest.raises(AdmissionError):
        admission.cancel(evidence)


def test_overflowed_deadline_is_rejected(gate):
    admission, clock = gate
    clock.value = 1e308
    with pytest.raises(ValueError):
        admission.close(timeout=1e308)
    admission.admit()


@pytest.mark.parametrize("identity,generation", [("", "generation"), ("start", "")])
def test_empty_identity_rejected(identity, generation):
    with pytest.raises(ValueError):
        PrestopAdmission(identity, generation)


def test_concurrent_cancel_and_commit_have_one_winner(gate):
    admission, _ = gate
    evidence = admission.close(timeout=10)
    barrier = threading.Barrier(2)

    def run(operation):
        barrier.wait(timeout=5)
        try:
            operation(evidence)
            return True
        except AdmissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, operation) for operation in (admission.commit, admission.cancel)
        ]
        assert sum(future.result(timeout=5) for future in futures) == 1
