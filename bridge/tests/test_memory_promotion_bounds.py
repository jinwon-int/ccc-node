"""Capacity-bound contracts for `CodexMemoryPromoter` (#578).

The promoter treats its two shared files **asymmetrically**, on purpose:

* `memory-facts.jsonl` is a bounded rolling window owned by
  `CodexLocalMemorySink`. Its primary writer truncates identically
  (`memory/distill_local_sink.py:202`, `(lines + added)[-self.max_facts:]`), so
  the promoter truncating too is consistent, not lossy-by-accident. Raising
  here would break promotion every time the ring filled — the opposite of the
  intent.
* `memory-promotion-audit.jsonl` fails closed, because an audit record is the
  only proof a promotion happened. Evicting one would destroy that proof
  silently.

The guard sits *before* both writes, so "fact written, audit missing" is
unreachable.

None of that was pinned. `test_memory_promotion.py`'s `_promoter()` helper
passes no limits, so every existing test runs at the defaults
(`max_facts=1000`, `max_audits=4000`) and cannot reach either bound; a
repo-wide search for `max_facts`/`max_audits` outside `promotion.py` finds only
production code. The similarly-named
`test_distill_local_sink.py::test_writes_bounded_private_facts_…` writes a
single fact, so the sibling bound is unpinned too.

These tests lock the asymmetry in as intended behaviour — they must not be
"fixed" into symmetry.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from telegram_bot.memory.promotion import CodexMemoryPromoter

from test_memory_promotion import (  # noqa: E402 — sibling suite, rootdir-relative
    FACT_ID,
    PRIVATE_SCOPE,
    _read_jsonl,
    _write_source,
)


def _promoter(root: Path, *, max_facts: int = 1000, max_audits: int = 4000):
    return CodexMemoryPromoter(
        root,
        max_facts=max_facts,
        max_audits=max_audits,
        now=lambda: datetime(2026, 7, 23, 2, 30, tzinfo=timezone.utc),
    )


def _seed_shared(root: Path, *, facts: int = 0, audits: int = 0) -> tuple[Path, Path]:
    """Pre-fill the shared files with `facts`/`audits` filler records.

    Filler ids are deliberately unlike the promoter's derived
    `promoted-…`/`promotion-…` ids so `_matching_records` treats this promotion
    as new rather than as an idempotent replay.
    """
    state = root / "shared" / "state"
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    # mkdir(parents=True) applies the ambient umask to intermediates; pin them
    # so this fixture also passes the umask-0002 CI variant (#770/#779).
    for component in (root, root / "shared", state):
        component.chmod(0o700)

    def _write(path: Path, count: int, prefix: str) -> Path:
        if count:
            path.write_text(
                "".join(
                    json.dumps({"id": f"{prefix}-{i:04d}"}, sort_keys=True) + "\n"
                    for i in range(count)
                )
            )
            path.chmod(0o600)
        return path

    return (
        _write(state / "memory-facts.jsonl", facts, "filler-fact"),
        _write(state / "memory-promotion-audit.jsonl", audits, "filler-audit"),
    )


# --- facts: silent drop-oldest truncation is the contract ------------------


def test_facts_at_capacity_evict_the_oldest_and_do_not_raise(tmp_path: Path) -> None:
    root = tmp_path / "audiences"
    _write_source(root)
    _seed_shared(root, facts=3)

    result = _promoter(root, max_facts=3).promote(
        source_scope=PRIVATE_SCOPE,
        fact_id=FACT_ID,
    )

    shared = _read_jsonl(root / "shared" / "state" / "memory-facts.jsonl")
    assert result.promoted is True
    assert len(shared) == 3
    # Drop-oldest, not drop-newest: filler-fact-0000 is gone, the promotion is
    # last, and the surviving filler keeps its relative order.
    assert [r["id"] for r in shared[:2]] == ["filler-fact-0001", "filler-fact-0002"]
    assert shared[-1]["id"] == result.destination_fact_id


def test_facts_below_capacity_simply_append(tmp_path: Path) -> None:
    root = tmp_path / "audiences"
    _write_source(root)
    _seed_shared(root, facts=1)

    _promoter(root, max_facts=3).promote(source_scope=PRIVATE_SCOPE, fact_id=FACT_ID)

    shared = _read_jsonl(root / "shared" / "state" / "memory-facts.jsonl")
    assert [r["id"] for r in shared[:1]] == ["filler-fact-0000"]
    assert len(shared) == 2


def test_max_facts_of_one_keeps_only_the_newest(tmp_path: Path) -> None:
    root = tmp_path / "audiences"
    _write_source(root)
    _seed_shared(root, facts=1)

    result = _promoter(root, max_facts=1).promote(
        source_scope=PRIVATE_SCOPE,
        fact_id=FACT_ID,
    )

    shared = _read_jsonl(root / "shared" / "state" / "memory-facts.jsonl")
    assert [r["id"] for r in shared] == [result.destination_fact_id]


# --- audits: fail closed ----------------------------------------------------


def test_audit_at_capacity_refuses_the_promotion(tmp_path: Path) -> None:
    root = tmp_path / "audiences"
    _write_source(root)
    _seed_shared(root, audits=3)

    with pytest.raises(ValueError) as caught:
        _promoter(root, max_audits=3).promote(
            source_scope=PRIVATE_SCOPE,
            fact_id=FACT_ID,
        )
    # Operator-facing text: it names the required action, so pin it.
    assert str(caught.value) == (
        "memory promotion audit capacity requires operator rotation"
    )


def test_audit_one_below_capacity_still_succeeds(tmp_path: Path) -> None:
    """Pins `>=`. Flipping the guard to `>` would let the ring evict an audit."""
    root = tmp_path / "audiences"
    _write_source(root)
    _seed_shared(root, audits=2)

    _promoter(root, max_audits=3).promote(source_scope=PRIVATE_SCOPE, fact_id=FACT_ID)

    audits = _read_jsonl(root / "shared" / "state" / "memory-promotion-audit.jsonl")
    assert len(audits) == 3
    assert [r["id"] for r in audits[:2]] == ["filler-audit-0000", "filler-audit-0001"]


def test_audit_refusal_leaves_the_facts_file_untouched(tmp_path: Path) -> None:
    """The load-bearing one.

    The capacity guard precedes both writes, so a refused promotion must not
    have written a fact. Without this assertion someone could "simplify" the
    guard to sit after the fact write and still pass everything above —
    producing a shared fact with no audit record proving where it came from.
    """
    root = tmp_path / "audiences"
    _write_source(root)
    facts_path, _ = _seed_shared(root, facts=1, audits=3)
    before = facts_path.read_bytes()

    with pytest.raises(ValueError):
        _promoter(root, max_facts=10, max_audits=3).promote(
            source_scope=PRIVATE_SCOPE,
            fact_id=FACT_ID,
        )

    assert facts_path.read_bytes() == before


def test_replay_of_an_existing_audit_skips_the_capacity_check(tmp_path: Path) -> None:
    """The subtlest branch: `not existing_audits and len(...) >= max`.

    An already-audited promotion is idempotent and must keep succeeding even
    once the audit log is full — otherwise a full log turns every replay
    (crash recovery, retry) into a hard failure.
    """
    root = tmp_path / "audiences"
    _write_source(root)
    promoter = _promoter(root, max_facts=10, max_audits=10)
    first = promoter.promote(source_scope=PRIVATE_SCOPE, fact_id=FACT_ID)

    # Now replay with a limit the existing audit log already meets.
    audits_path = root / "shared" / "state" / "memory-promotion-audit.jsonl"
    assert len(_read_jsonl(audits_path)) == 1
    replay = _promoter(root, max_facts=10, max_audits=1).promote(
        source_scope=PRIVATE_SCOPE,
        fact_id=FACT_ID,
    )

    assert replay.destination_fact_id == first.destination_fact_id
    assert len(_read_jsonl(audits_path)) == 1


# --- constructor validation -------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limits_are_rejected(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="max_facts must be a positive integer"):
        CodexMemoryPromoter(tmp_path, max_facts=limit)
    with pytest.raises(ValueError, match="max_audits must be a positive integer"):
        CodexMemoryPromoter(tmp_path, max_audits=limit)


def test_bool_limits_are_rejected(tmp_path: Path) -> None:
    """`type(x) is not int` is deliberate — `isinstance(True, int)` is True.

    `max_facts=True` would otherwise become a silent limit of 1, quietly
    discarding every shared fact but the newest.
    """
    with pytest.raises(ValueError, match="max_facts must be a positive integer"):
        CodexMemoryPromoter(tmp_path, max_facts=True)
    with pytest.raises(ValueError, match="max_audits must be a positive integer"):
        CodexMemoryPromoter(tmp_path, max_audits=True)
