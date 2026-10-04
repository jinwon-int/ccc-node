"""Queue fairness, context invalidation, and cross-channel daily call limits."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


@pytest.fixture
def judge(monkeypatch, tmp_path):
    hook = Path(__file__).resolve().parents[2] / "claude/hooks/nunchi/judge-batch.py"
    monkeypatch.setenv("NUNCHI_HOME", str(tmp_path))
    monkeypatch.setenv("NUNCHI_DB", str(tmp_path / "facts.db"))
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NUNCHI_G3_DUP_THRESHOLD", "1.0")
    spec = importlib.util.spec_from_file_location("nunchi_test_judge", hook)
    module = importlib.util.module_from_spec(spec)
    original = list(sys.path)
    spec.loader.exec_module(module)
    sys.path[:] = original
    monkeypatch.setattr(module, "CAP", 1)
    monkeypatch.setattr(module, "DUP_THRESHOLD", 1.0)
    return module


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "facts.db")
    connection.execute("""CREATE TABLE peer_facts(
        id INTEGER PRIMARY KEY, observed TEXT, kind TEXT, fact TEXT, source_rank INTEGER,
        created_at TEXT, because TEXT, evidence TEXT, valid_from TEXT, valid_to TEXT, review INTEGER)""")
    for ident in range(1, 5):
        connection.execute("INSERT INTO peer_facts VALUES(?,?,?,?,?,?,?,?,?,NULL,1)",
                           (ident, "session:fixture", "fact", "shared common words content " + str(ident),
                            1, "2020-01-01T00:00:00+00:00", None, "fixture", "2020-01-01"))
    connection.commit()
    yield connection
    connection.close()


def test_unchanged_human_hold_does_not_starve_later_rows_and_reason_change_reopens(judge, conn):
    stamp = datetime.now(timezone.utc).timestamp()
    first, _, _ = judge.fetch_queue(conn)
    assert first[0][0] == 1
    judge.judge_state.persist(conn, [dict(id=1, verdict="human", backend="danso")],
                              judge.QUEUE_FINGERPRINTS, judge.QUEUE_STATE, stamp)
    second, _, _ = judge.fetch_queue(conn)
    assert second[0][0] == 2
    assert judge.QUEUE_COUNTS["human_hold"] == 1
    # A sibling's supported reason changing is new evidence, even with no text edit.
    conn.execute("UPDATE peer_facts SET because='new source reason' WHERE id=4")
    third, _, _ = judge.fetch_queue(conn)
    assert third[0][0] == 1


def test_backend_failure_retries_with_bounded_backoff_and_does_not_clear(judge, conn):
    stamp = datetime.now(timezone.utc).timestamp()
    judge.fetch_queue(conn)
    verdict = dict(id=1, verdict="human", backend=None, **{"class": "judge"})
    judge.judge_state.persist(conn, [verdict], judge.QUEUE_FINGERPRINTS, {}, stamp)
    state = judge.judge_state.load(conn)
    assert state[1][1:] == ("retry", stamp, stamp + 900, 1)
    judge.judge_state.persist(conn, [verdict], judge.QUEUE_FINGERPRINTS, state, stamp + 900)
    assert judge.judge_state.load(conn)[1][3] == stamp + 900 + 1800
    assert conn.execute("SELECT review FROM peer_facts WHERE id=1").fetchone()[0] == 1


def test_mutation_rechecks_context_after_provider_call(judge, conn):
    judge.fetch_queue(conn)
    conn.execute("UPDATE peer_facts SET fact='changed policy evidence' WHERE id=4")
    assert not judge.apply_clear(conn, 1, dict(verdict="clear"))
    assert conn.execute("SELECT review FROM peer_facts WHERE id=1").fetchone()[0] == 1


def test_backup_retains_committed_wal_rows(judge, conn, monkeypatch, tmp_path):
    monkeypatch.setattr(judge, "BACKUP_DIR", str(tmp_path / "backup"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("UPDATE peer_facts SET because='committed in WAL' WHERE id=1")
    conn.commit()
    backup = judge.backup_db()
    restored = sqlite3.connect(backup)
    try:
        assert restored.execute("SELECT because FROM peer_facts WHERE id=1").fetchone()[0] == "committed in WAL"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()


def test_shared_budget_concurrency_midnight_and_history(judge, tmp_path):
    root = tmp_path / "shared"
    day = datetime(2026, 10, 4, 14, 59, tzinfo=timezone.utc)
    def reserve(_):
        return judge.judge_state.call_budget(root, 5, moment=day)[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(20))) == 5
    tomorrow = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
    assert judge.judge_state.call_budget(root, 5, moment=tomorrow) == (True, 4)
    data = json.loads((root / "nunchi-judge/calls.json").read_text())
    assert data == {"2026-10-04": 5, "2026-10-05": 1}
    assert (root / "nunchi-judge/calls.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("target", ["calls.json", ".calls.lock"])
def test_budget_refuses_symlink_without_touching_target(judge, tmp_path, target):
    root = tmp_path / "safe"
    directory = root / "nunchi-judge"
    directory.mkdir(parents=True, mode=0o700)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text('{"2026-10-04": 123}')
    sentinel.chmod(0o600)
    (directory / target).symlink_to(sentinel)
    with pytest.raises((OSError, ValueError, judge.ccc_secure_fs.SecureFsError)):
        judge.judge_state.call_budget(root, 5)
    assert sentinel.read_text() == '{"2026-10-04": 123}'


def test_each_fallback_consumes_a_shared_call_and_exhaustion_stops_dispatch(judge, monkeypatch, tmp_path):
    monkeypatch.setattr(judge, "DAILY_CAP", 1)
    monkeypatch.setattr(judge, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(judge, "judge_candidates", lambda: [("claude", "fake"), ("codex", "fake")])
    monkeypatch.setattr(judge, "candidate_available", lambda *args: True)
    calls = []
    monkeypatch.setattr(judge, "_claude_judge", lambda *args: (calls.append("claude") or None, "timeout"))
    monkeypatch.setattr(judge, "_codex_judge", lambda *args: pytest.fail("cap bypassed by fallback"))
    item = (1, "session:fixture", "fact", "synthetic policy", 1, "2020-01-01", None)
    result = judge.judge_item(item, [(2, "synthetic sibling", 0.7)])
    assert result["backend"] is None and calls == ["claude"]
    assert result["attempts"][-1] == "budget:exhausted"


def test_quota_wait_is_not_a_provider_failure_and_reopens_at_kst_midnight(judge, conn, monkeypatch):
    monkeypatch.setattr(judge, "judge_available", lambda: True)
    monkeypatch.setattr(judge, "judge_item", lambda *args: dict(
        verdict="human", rationale="bounded wait", supersede_proposal=None,
        backend=None, attempts=["budget:exhausted"]))
    queue, _, _ = judge.fetch_queue(conn)
    decisions = judge.triage_queue(conn, queue)
    assert decisions[0]["class"] == "budget-deferred"
    stamp = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc).timestamp()  # 18:00 KST
    prior = {1: (judge.QUEUE_FINGERPRINTS[1], "retry", stamp - 3600, stamp, 7)}
    judge.judge_state.persist(conn, decisions, judge.QUEUE_FINGERPRINTS, prior, stamp)
    record = judge.judge_state.load(conn)[1]
    assert record[1] == "budget" and record[4] == 7
    assert record[3] == datetime(2026, 10, 4, 15, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("first_available", [False, True])
def test_auto_fallback_distinguishes_skipped_cli_from_real_failed_call(judge, conn, monkeypatch, first_available):
    monkeypatch.setattr(judge, "judge_candidates", lambda: [("claude", "fake"), ("codex", "fake")])
    monkeypatch.setattr(judge, "candidate_available", lambda provider, command: first_available or provider == "codex")
    budgets = iter([(True, 0), (False, 0)] if first_available else [(False, 0)])
    monkeypatch.setattr(judge.judge_state, "call_budget", lambda *args, **kwargs: next(budgets))
    monkeypatch.setattr(judge, "_claude_judge", lambda *args: (None, "timeout"))
    monkeypatch.setattr(judge, "_codex_judge", lambda *args: pytest.fail("exhausted budget invoked Codex"))
    queue, _, _ = judge.fetch_queue(conn)
    decision = judge.triage_queue(conn, queue)[0]
    assert decision["provider_calls"] == int(first_available)
    assert decision["class"] == ("judge" if first_available else "budget-deferred")
    stamp = datetime(2026, 10, 4, 9, tzinfo=timezone.utc).timestamp()
    prior = {1: (judge.QUEUE_FINGERPRINTS[1], "retry", stamp - 3600, stamp, 7)}
    judge.judge_state.persist(conn, [decision], judge.QUEUE_FINGERPRINTS, prior, stamp)
    state = judge.judge_state.load(conn)[1]
    assert state[4] == (8 if first_available else 7)
    assert state[3] == stamp + (86400 if first_available else 6 * 3600)
