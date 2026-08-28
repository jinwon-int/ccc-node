"""Tests for the durable async-completion journal (#646 slice 1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from telegram_bot.core.async_completion_event import (
    NormalizedAsyncCompletionEvent,
)
from telegram_bot.core.async_completion_journal import (
    AsyncCompletionJournal,
)


def _event(
    turn_id: str = "turn-1111",
    thread_id: str = "thread-aaaa",
    generation: int = 3,
) -> NormalizedAsyncCompletionEvent:
    return NormalizedAsyncCompletionEvent(
        provider="codex",
        thread_id=thread_id,
        conversation_route_id="7360371189",
        session_generation=generation,
        turn_id=turn_id,
    )


def _frozen_clock(start: datetime):
    """Return a clock advancing one minute per call from ``start``."""

    state = {"now": start, "calls": 0}

    def clock() -> datetime:
        state["now"] = start + timedelta(minutes=state["calls"])
        state["calls"] += 1
        return state["now"]

    return clock


def _make_journal(tmp_path, *, max_records: int = 256) -> AsyncCompletionJournal:
    journal = AsyncCompletionJournal(
        tmp_path / "async-completions", max_records=max_records
    )
    journal.initialize()
    return journal


class TestObserve:
    def test_first_observation_queues_and_returns_true(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()

        assert journal.observe(event) is True
        record = journal.get(event.idempotency_key)
        assert record is not None
        assert record.state == "queued"
        assert record.provider == "codex"
        assert record.session_generation == 3
        assert record.attempts == 0

    def test_duplicate_observation_is_exactly_once(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()

        assert journal.observe(event) is True
        for _ in range(10):
            assert journal.observe(event) is False

        records = journal.list_records()
        assert len(records) == 1

    def test_distinct_identities_get_distinct_records(self, tmp_path):
        journal = _make_journal(tmp_path)

        assert journal.observe(_event(turn_id="turn-1111")) is True
        assert journal.observe(_event(turn_id="turn-2222")) is True
        assert len(journal.list_records()) == 2

    def test_record_file_stays_body_free(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event(turn_id="turn-secret-1", thread_id="thread-secret-1")
        journal.observe(event)

        record_id = journal.record_id_for(event.idempotency_key)
        payload = json.loads(
            (journal.root / f"{record_id}.json").read_text(encoding="utf-8")
        )
        serialized = json.dumps(payload)
        assert "turn-secret-1" not in serialized
        assert "thread-secret-1" not in serialized
        assert payload["idempotency_key"] == event.idempotency_key

    def test_rejects_non_normalized_event(self, tmp_path):
        journal = _make_journal(tmp_path)
        with pytest.raises(ValueError):
            journal.observe({"provider": "codex"})  # type: ignore[arg-type]


class TestStateTransitions:
    def test_happy_path_claim_then_deliver(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)

        claimed = journal.mark(event.idempotency_key, "claimed")
        assert claimed.attempts == 1
        delivered = journal.mark(event.idempotency_key, "delivered")
        assert delivered.state == "delivered"
        assert delivered.terminal is True

    def test_retry_path_increments_attempts(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)
        journal.mark(event.idempotency_key, "claimed")
        retried = journal.mark(
            event.idempotency_key, "retryable_failed", error_code="send_timeout"
        )
        assert retried.last_error_code == "send_timeout"
        journal.mark(event.idempotency_key, "claimed")
        terminal = journal.mark(
            event.idempotency_key, "terminal_failed", error_code="send_timeout"
        )
        assert terminal.state == "terminal_failed"
        record = journal.get(event.idempotency_key)
        assert record is not None and record.attempts == 2

    def test_illegal_transition_fails_closed(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)

        with pytest.raises(ValueError):
            journal.mark(event.idempotency_key, "delivered")

    def test_terminal_state_is_final(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)
        journal.mark(event.idempotency_key, "claimed")
        journal.mark(event.idempotency_key, "delivered")

        with pytest.raises(ValueError):
            journal.mark(event.idempotency_key, "claimed")

    def test_invalid_error_code_rejected(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)
        journal.mark(event.idempotency_key, "claimed")

        with pytest.raises(ValueError):
            journal.mark(
                event.idempotency_key,
                "retryable_failed",
                error_code="NOT A CODE",
            )


class TestRecoveryAndRetention:
    def test_stale_claim_recovers_to_queued(self, tmp_path):
        start = datetime(2026, 8, 28, tzinfo=timezone.utc)
        clock = _frozen_clock(start)
        journal = AsyncCompletionJournal(
            tmp_path / "async-completions",
            clock=clock,
            stale_claim_seconds=300.0,
        )
        journal.initialize()
        event = _event()
        journal.observe(event)
        journal.mark(event.idempotency_key, "claimed")

        # Claim is fresh: nothing to recover yet.
        assert journal.recover_stale_claimed() == ()
        # Advance past the staleness bound (clock moves per call).
        for _ in range(6):
            clock()
        recovered = journal.recover_stale_claimed()
        assert len(recovered) == 1
        assert recovered[0].state == "queued"
        record = journal.get(event.idempotency_key)
        assert record is not None and record.state == "queued"

    def test_fresh_claim_is_not_recovered(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)
        journal.mark(event.idempotency_key, "claimed")

        assert journal.recover_stale_claimed() == ()

    def test_noticed_stamp_is_idempotent(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)

        first = journal.mark_noticed(event.idempotency_key)
        second = journal.mark_noticed(event.idempotency_key)
        assert first.noticed_at == second.noticed_at

    def test_retention_evicts_oldest_terminal_only(self, tmp_path):
        journal = _make_journal(tmp_path, max_records=3)
        live = _event(turn_id="turn-live", generation=1)
        journal.observe(live)
        for index in range(3):
            event = _event(turn_id=f"turn-old-{index}", generation=1)
            journal.observe(event)
            journal.mark(event.idempotency_key, "claimed")
            journal.mark(event.idempotency_key, "delivered")

        # One new observation forces eviction of the oldest terminal record.
        newest = _event(turn_id="turn-new", generation=1)
        assert journal.observe(newest) is True
        records = journal.list_records()
        assert len(records) <= 4
        assert all(record.idempotency_key != _event(
            turn_id="turn-old-0", generation=1
        ).idempotency_key for record in records)
        # The live (non-terminal) record always survives retention.
        assert journal.get(live.idempotency_key) is not None


class TestRouteBinding:
    """Schema-2 route-bound deliverable records (#646 slice 2)."""

    def test_deliverable_observation_binds_route(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()

        assert journal.observe(event, deliverable=True) is True
        record = journal.get(event.idempotency_key)
        assert record is not None
        assert record.conversation_route_id == "7360371189"

    def test_plain_observation_stays_route_free(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)
        record = journal.get(event.idempotency_key)
        assert record is not None
        assert record.conversation_route_id is None

    def test_duplicate_deliverable_observation_changes_nothing(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        assert journal.observe(event, deliverable=True) is True
        assert journal.observe(event) is False
        assert journal.observe(event, deliverable=True) is False
        record = journal.get(event.idempotency_key)
        assert record is not None
        assert record.conversation_route_id == "7360371189"

    def test_malformed_route_is_rejected(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = NormalizedAsyncCompletionEvent(
            provider="codex",
            thread_id="thread-aaaa",
            conversation_route_id="not-a-route",
            session_generation=3,
            turn_id="turn-1111",
        )
        with pytest.raises(ValueError):
            journal.observe(event, deliverable=True)

    def test_list_deliverable_queued_filters_and_orders(self, tmp_path):
        journal = _make_journal(tmp_path)
        deliverable = _event(turn_id="turn-2222")
        evidence = _event(turn_id="turn-3333")
        journal.observe(deliverable, deliverable=True)
        journal.observe(evidence)
        assert [r.idempotency_key for r in journal.list_deliverable_queued()] == [
            deliverable.idempotency_key
        ]
        journal.mark(deliverable.idempotency_key, "claimed")
        assert journal.list_deliverable_queued() == ()

    def test_list_route_bound_includes_retryable_and_filters_states(
        self, tmp_path
    ):
        journal = _make_journal(tmp_path)
        queued = _event(turn_id="turn-2222")
        retryable = _event(turn_id="turn-3333")
        delivered = _event(turn_id="turn-4444")
        evidence = _event(turn_id="turn-5555")
        journal.observe(queued, deliverable=True)
        journal.observe(retryable, deliverable=True)
        journal.observe(delivered, deliverable=True)
        journal.observe(evidence)
        journal.mark(retryable.idempotency_key, "claimed")
        journal.mark(
            retryable.idempotency_key, "retryable_failed", error_code="x"
        )
        journal.mark(delivered.idempotency_key, "claimed")
        journal.mark(delivered.idempotency_key, "delivered")

        both = journal.list_route_bound(frozenset({"queued", "retryable_failed"}))
        assert [r.idempotency_key for r in both] == [
            queued.idempotency_key,
            retryable.idempotency_key,
        ]
        queued_only = journal.list_deliverable_queued()
        assert [r.idempotency_key for r in queued_only] == [
            queued.idempotency_key
        ]

    def test_list_route_bound_rejects_unknown_state(self, tmp_path):
        journal = _make_journal(tmp_path)
        with pytest.raises(ValueError):
            journal.list_route_bound(frozenset({"bogus_state"}))

    def test_parse_route_private_and_group(self, tmp_path):
        journal = _make_journal(tmp_path)
        private = _event(turn_id="turn-2222")
        journal.observe(private, deliverable=True)
        record = journal.get(private.idempotency_key)
        assert record is not None
        assert AsyncCompletionJournal.parse_route(record) == (7360371189, 7360371189)

        group = _event(turn_id="turn-3333")
        group = NormalizedAsyncCompletionEvent(
            provider="codex",
            thread_id="thread-aaaa",
            conversation_route_id="111:222",
            session_generation=3,
            turn_id="turn-3333",
        )
        journal.observe(group, deliverable=True)
        record = journal.get(group.idempotency_key)
        assert record is not None
        assert AsyncCompletionJournal.parse_route(record) == (111, 222)

    def test_parse_route_evidence_only_is_none(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event)
        record = journal.get(event.idempotency_key)
        assert record is not None
        assert AsyncCompletionJournal.parse_route(record) is None

    def test_v1_payload_without_route_still_decodes(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event, deliverable=True)
        record_id = journal.record_id_for(event.idempotency_key)
        path = journal.root / f"{record_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Downgrade to a legacy schema-1 record exactly as slice 1 wrote it.
        payload["schema_version"] = 1
        payload.pop("conversation_route_id")
        path.write_text(json.dumps(payload), encoding="utf-8")

        record = journal.get(event.idempotency_key)
        assert record is not None
        assert record.conversation_route_id is None

    def test_v1_payload_with_route_is_rejected(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event()
        journal.observe(event, deliverable=True)
        record_id = journal.record_id_for(event.idempotency_key)
        path = journal.root / f"{record_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError):
            journal.get(event.idempotency_key)

    def test_route_bound_record_stays_body_free(self, tmp_path):
        journal = _make_journal(tmp_path)
        event = _event(turn_id="turn-secret-2", thread_id="thread-secret-2")
        journal.observe(event, deliverable=True)
        record_id = journal.record_id_for(event.idempotency_key)
        serialized = (journal.root / f"{record_id}.json").read_text(
            encoding="utf-8"
        )
        assert "turn-secret-2" not in serialized
        assert "thread-secret-2" not in serialized


class TestRecordIdMapping:
    def test_record_id_for_rejects_foreign_keys(self):
        with pytest.raises(ValueError):
            AsyncCompletionJournal.record_id_for("usage-budget:whatever")
        with pytest.raises(ValueError):
            AsyncCompletionJournal.record_id_for("async-completion:zz")

    def test_record_id_for_accepts_identity_hashes(self):
        event = _event()
        record_id = AsyncCompletionJournal.record_id_for(event.idempotency_key)
        assert len(record_id) == 64
        int(record_id, 16)
