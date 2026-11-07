"""Tests for the local OTel-GenAI-named span ledger (#2684, slice 1).

Mirrors the cost-ledger contract (``test_usage_cost_ledger.py``): append-only
JSONL, corrupt lines skipped on read, every write failure swallowed so the
conversation path is never blocked — plus the span-specific guarantees:
body-free allowlist, owner-only files, and a retention prune that only ever
deletes exact ``YYYY-MM-DD.jsonl`` files and never follows symlinks.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core import span_ledger as mod
from telegram_bot.core.span_ledger import (
    DEFAULT_RETENTION_DAYS,
    SpanLedger,
    conversation_ref,
    iter_spans,
    sanitize_span,
    turn_span_from_claude_result,
)
from telegram_bot.core.usage import ModelUsage, UsageSnapshot
from telegram_bot.utils.redaction import REDACTION_MARKER

# 2023-11-14T22:13:20Z
BASE = 1_700_000_000.0
BASE_DAY = "2023-11-14"


def _ledger(tmp_path: Path, **kwargs) -> SpanLedger:
    kwargs.setdefault("clock", lambda: BASE)
    kwargs.setdefault("node", "testnode")
    kwargs.setdefault("channel", "telegram")
    return SpanLedger(tmp_path / "spans", **kwargs)


def _turn(**overrides) -> dict:
    span = {
        "kind": "turn",
        "status": "ok",
        "start_ts": BASE - 2.5,
        "duration_ms": 2500,
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.request.model": "claude-opus-4-5",
        "gen_ai.usage.input_tokens": 120,
        "gen_ai.usage.output_tokens": 7,
    }
    span.update(overrides)
    return span


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ---- writes -----------------------------------------------------------------


def test_writes_one_line_per_span_into_the_utc_day_file(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    assert ledger.record(_turn()) is True
    assert ledger.record(_turn(status="error", **{"error.type": "error_max_turns"})) is True

    path = tmp_path / "spans" / f"{BASE_DAY}.jsonl"
    lines = _lines(path)
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "turn"
    assert first["status"] == "ok"
    assert first["gen_ai.request.model"] == "claude-opus-4-5"
    assert first["gen_ai.usage.input_tokens"] == 120
    # Ledger-level defaults are stamped on every span.
    assert first["ccc.node"] == "testnode"
    assert first["ccc.channel"] == "telegram"
    assert json.loads(lines[1])["error.type"] == "error_max_turns"


def test_day_file_follows_the_utc_date_of_the_clock(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 6, 23, 59, 59, tzinfo=timezone.utc).timestamp()]
    ledger = _ledger(tmp_path, clock=lambda: now[0])
    ledger.record(_turn())
    now[0] += 2  # crosses UTC midnight
    ledger.record(_turn())

    assert sorted(p.name for p in (tmp_path / "spans").iterdir()) == [
        "2026-11-06.jsonl",
        "2026-11-07.jsonl",
    ]


def test_files_are_owner_only(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(_turn())

    spans_dir = tmp_path / "spans"
    assert stat.S_IMODE(spans_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((spans_dir / f"{BASE_DAY}.jsonl").stat().st_mode) == 0o600


def test_disabled_ledger_writes_nothing(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, enabled=False)
    assert ledger.record(_turn()) is False
    assert not (tmp_path / "spans").exists()


def test_invalid_kind_or_status_is_not_written(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.record(_turn(kind="prompt")) is False
    assert ledger.record(_turn(status="weird")) is False
    assert ledger.record({"status": "ok"}) is False  # no kind at all
    assert not (tmp_path / "spans" / f"{BASE_DAY}.jsonl").exists()


# ---- reader -----------------------------------------------------------------


def test_reader_skips_corrupt_lines(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(_turn())
    path = tmp_path / "spans" / f"{BASE_DAY}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write("[1, 2, 3]\n")
        handle.write('{"no_kind": true}\n')
        handle.write("\n")
    ledger.record(_turn(status="timeout"))
    with path.open("ab") as handle:
        handle.write(b'{"kind": "turn", "status": "ok", "trunc')  # torn tail

    spans = list(ledger.read(BASE_DAY))
    assert [span["status"] for span in spans] == ["ok", "timeout"]
    assert list(iter_spans(path)) == spans


def test_reader_on_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert list(_ledger(tmp_path).read("2020-01-01")) == []
    assert list(iter_spans(tmp_path / "nope.jsonl")) == []


# ---- concurrency ------------------------------------------------------------


def test_concurrent_thread_appends_produce_intact_lines(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    threads_n, per_thread = 8, 50

    def worker(index: int) -> None:
        for turn in range(per_thread):
            ledger.record(_turn(**{"ccc.turn_id": f"t{index}-{turn}"}))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = _lines(tmp_path / "spans" / f"{BASE_DAY}.jsonl")
    assert len(lines) == threads_n * per_thread
    ids = {json.loads(line)["ccc.turn_id"] for line in lines}
    assert len(ids) == threads_n * per_thread


def _process_worker(root: str, index: int, count: int) -> None:
    ledger = SpanLedger(root, clock=lambda: BASE, node="n", channel="matrix")
    for turn in range(count):
        ledger.record(_turn(**{"ccc.turn_id": f"p{index}-{turn}"}))


def test_concurrent_process_appends_produce_intact_lines(tmp_path: Path) -> None:
    # A Telegram and a Matrix frontend on one project root share this dir.
    ctx = multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")
    procs_n, per_proc = 4, 100
    procs = [
        ctx.Process(target=_process_worker, args=(str(tmp_path / "spans"), i, per_proc))
        for i in range(procs_n)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(30)
        assert proc.exitcode == 0

    lines = _lines(tmp_path / "spans" / f"{BASE_DAY}.jsonl")
    assert len(lines) == procs_n * per_proc
    parsed = [json.loads(line) for line in lines]  # every line intact JSON
    assert len({span["ccc.turn_id"] for span in parsed}) == procs_n * per_proc


# ---- failure swallowing -----------------------------------------------------


def test_unwritable_location_is_swallowed(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    ledger = SpanLedger(blocker / "spans", clock=lambda: BASE)
    assert ledger.record(_turn()) is False  # no exception


def test_write_error_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "write", boom)
    assert ledger.record(_turn()) is False


def test_bad_values_and_bad_inputs_never_raise(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert ledger.record(None) is False  # type: ignore[arg-type]
    assert ledger.record("not a mapping") is False  # type: ignore[arg-type]
    assert ledger.record(_turn(**{"gen_ai.usage.input_tokens": Hostile()})) is True
    assert ledger.record(_turn(duration_ms=float("nan"), start_ts=float("inf"))) is True
    spans = list(ledger.read(BASE_DAY))
    assert "gen_ai.usage.input_tokens" not in spans[0]
    assert "duration_ms" not in spans[1] and "start_ts" not in spans[1]


def test_hostile_clock_is_swallowed(tmp_path: Path) -> None:
    def clock() -> float:
        raise RuntimeError("clock broke")

    assert _ledger(tmp_path, clock=clock).record(_turn()) is False


def test_symlinked_day_file_is_not_followed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record(_turn())  # creates the dir
    target = tmp_path / "victim.txt"
    target.write_text("keep\n", encoding="utf-8")
    day_file = tmp_path / "spans" / "2023-11-15.jsonl"
    day_file.symlink_to(target)

    later = _ledger(tmp_path, clock=lambda: BASE + 86400)
    assert later.record(_turn()) is False
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_symlinked_spans_dir_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "spans").symlink_to(real, target_is_directory=True)
    assert _ledger(tmp_path).record(_turn()) is False
    assert list(real.iterdir()) == []


# ---- body-free --------------------------------------------------------------


def test_unknown_keys_and_prompt_like_text_are_dropped() -> None:
    clean = sanitize_span(
        _turn(
            prompt="please summarise my bank statement",
            **{
                "gen_ai.prompt": "hello",
                "gen_ai.completion": "world",
                "tool_input": {"path": "/etc/passwd"},
                "ccc.skill": "summarise the attached document for me",
                "gen_ai.request.model": ["nested", "list"],
            },
        )
    )
    assert clean is not None
    for key in ("prompt", "gen_ai.prompt", "gen_ai.completion", "tool_input"):
        assert key not in clean
    # Allowlisted key, but the value has spaces: free text, not an identifier.
    assert "ccc.skill" not in clean
    # Allowlisted key with a non-scalar value.
    assert "gen_ai.request.model" not in clean
    assert set(clean) <= mod.ALLOWED_KEYS


def test_long_strings_are_capped() -> None:
    clean = sanitize_span(_turn(**{"ccc.skill": "a" * 5000}))
    assert clean is not None
    assert len(clean["ccc.skill"]) == mod.MAX_STRING_CHARS


def test_secret_like_value_is_redacted() -> None:
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    clean = sanitize_span(_turn(**{"gen_ai.request.model": secret, "ccc.skill": "ghp_" + "x" * 30}))
    assert clean is not None
    assert clean["gen_ai.request.model"] == REDACTION_MARKER
    assert clean["ccc.skill"] == REDACTION_MARKER
    assert secret not in json.dumps(clean)


def test_numeric_fields_are_validated() -> None:
    clean = sanitize_span(
        _turn(
            **{
                "gen_ai.usage.input_tokens": -5,
                "gen_ai.usage.output_tokens": True,
                "duration_ms": "12",
            }
        )
    )
    assert clean is not None
    assert "gen_ai.usage.input_tokens" not in clean
    assert "gen_ai.usage.output_tokens" not in clean
    assert "duration_ms" not in clean


def test_written_line_carries_no_body_text(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    body = "UNIQUE-BODY-MARKER the user's private prompt text"
    ledger.record(_turn(prompt=body, result=body, **{"ccc.outcome": body}))
    raw = (tmp_path / "spans" / f"{BASE_DAY}.jsonl").read_text(encoding="utf-8")
    assert "UNIQUE-BODY-MARKER" not in raw


def test_conversation_ref_is_opaque_and_stable() -> None:
    ref = conversation_ref("claude-session-123")
    assert ref == conversation_ref("claude-session-123")
    assert ref != conversation_ref("claude-session-124")
    assert "claude-session" not in (ref or "")
    assert len(ref or "") == 16
    assert conversation_ref("") is None and conversation_ref(None) is None


# ---- retention prune --------------------------------------------------------


def _touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_prune_deletes_only_old_exactly_named_files(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc).timestamp()
    ledger = _ledger(tmp_path, clock=lambda: now, retention_days=14)
    spans = tmp_path / "spans"
    spans.mkdir(mode=0o700)
    old = _touch(spans, "2026-10-23.jsonl")  # 14 days old: past retention
    older = _touch(spans, "2025-01-01.jsonl")
    keep_edge = _touch(spans, "2026-10-24.jsonl")  # 13 days old: kept
    keep_today = _touch(spans, "2026-11-06.jsonl")
    lookalikes = [
        _touch(spans, "2026-10-01.jsonl.bak"),
        _touch(spans, "x2026-10-01.jsonl"),
        _touch(spans, "2026-10-01.json"),
        _touch(spans, "2026-13-45.jsonl"),  # matches shape, not a real date
        _touch(spans, "notes.txt"),
    ]
    (spans / "2026-01-01.jsonl").mkdir()  # a directory with a matching name

    assert ledger.prune() == 2
    assert not old.exists() and not older.exists()
    assert keep_edge.exists() and keep_today.exists()
    assert all(path.exists() for path in lookalikes)
    assert (spans / "2026-01-01.jsonl").is_dir()


def test_prune_never_follows_symlinks(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc).timestamp()
    ledger = _ledger(tmp_path, clock=lambda: now)
    spans = tmp_path / "spans"
    spans.mkdir(mode=0o700)
    victim = tmp_path / "victim.jsonl"
    victim.write_text("keep\n", encoding="utf-8")
    link = spans / "2020-01-01.jsonl"
    link.symlink_to(victim)

    assert ledger.prune() == 0
    assert victim.exists()
    assert link.is_symlink()  # the link itself is left alone too


def test_prune_refuses_a_symlinked_spans_dir(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc).timestamp()
    real = tmp_path / "real"
    real.mkdir()
    old = _touch(real, "2020-01-01.jsonl")
    (tmp_path / "spans").symlink_to(real, target_is_directory=True)

    assert _ledger(tmp_path, clock=lambda: now).prune() == 0
    assert old.exists()


def test_prune_runs_at_most_once_per_utc_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [datetime(2026, 11, 6, 1, tzinfo=timezone.utc).timestamp()]
    ledger = _ledger(tmp_path, clock=lambda: now[0])
    calls: list[float] = []
    real_prune = SpanLedger.prune

    def counting(self, *, now: float | None = None) -> int:
        calls.append(now or 0.0)
        return real_prune(self, now=now)

    monkeypatch.setattr(SpanLedger, "prune", counting)
    for _ in range(5):
        ledger.record(_turn())
    assert len(calls) == 1
    now[0] += 86400
    ledger.record(_turn())
    ledger.record(_turn())
    assert len(calls) == 2


def test_prune_failure_is_swallowed_and_not_retried_every_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    calls = []

    def boom(_path):
        calls.append(1)
        raise OSError("scandir failed")

    monkeypatch.setattr(mod.os, "scandir", boom)
    assert ledger.record(_turn()) is True
    assert ledger.record(_turn()) is True
    assert len(calls) == 1


def test_default_retention_is_fourteen_days() -> None:
    assert DEFAULT_RETENTION_DAYS == 14
    assert SpanLedger("/nonexistent", enabled=False).retention_days == 14
    assert SpanLedger("/nonexistent", retention_days=0).retention_days == 1


# ---- turn span mapping --------------------------------------------------------


def _result(**overrides):
    message = SimpleNamespace(
        subtype="success",
        duration_ms=4200,
        is_error=False,
        session_id="claude-session-abc",
        uuid="5e0c5a7e-1111-2222-3333-444455556666",
        result="SECRET ANSWER BODY",
    )
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _delta() -> UsageSnapshot:
    return UsageSnapshot(
        provider="claude",
        input_tokens=10,
        output_tokens=50,
        total_cost_usd=0.25,
        models=(
            ModelUsage(model="claude-haiku-4-5", input_tokens=3, output_tokens=2, cost_usd=0.01),
            ModelUsage(
                model="claude-opus-4-5",
                input_tokens=10,
                cache_creation_input_tokens=100,
                cache_read_input_tokens=1000,
                output_tokens=48,
                cost_usd=0.24,
            ),
        ),
    )


def test_turn_span_from_successful_claude_result() -> None:
    span = turn_span_from_claude_result(_result(), _delta(), end_ts=BASE)
    clean = sanitize_span(span)
    assert clean is not None
    assert clean["kind"] == "turn"
    assert clean["status"] == "ok"
    assert clean["gen_ai.operation.name"] == "invoke_agent"
    # The model that produced the most output is the turn's model.
    assert clean["gen_ai.request.model"] == "claude-opus-4-5"
    # OTel GenAI: input tokens include cached input.
    assert clean["gen_ai.usage.input_tokens"] == 3 + 10 + 100 + 1000
    assert clean["gen_ai.usage.output_tokens"] == 50
    assert clean["ccc.usage.cache_read_tokens"] == 1000
    assert clean["ccc.usage.cache_creation_tokens"] == 100
    assert clean["ccc.cost_usd"] == pytest.approx(0.25)
    assert clean["duration_ms"] == 4200
    assert clean["start_ts"] == pytest.approx(BASE - 4.2)
    assert clean["ccc.turn_id"] == "5e0c5a7e-1111-2222-3333-444455556666"
    assert clean["ccc.outcome"] == "success"
    assert clean["gen_ai.conversation.id"] == conversation_ref("claude-session-abc")
    assert "error.type" not in clean
    assert "SECRET ANSWER BODY" not in json.dumps(clean)
    assert "claude-session-abc" not in json.dumps(clean)


def test_turn_span_output_tokens_sum_all_models_not_main_loop_only() -> None:
    """#2684 slice 4: the SDK's top-level ``usage.output_tokens`` covers the
    main loop only, while ``model_usage`` also lists auxiliary/subagent models.
    The span must sum per-model output (as input and the cost ledger do), so
    span and ledger totals agree."""

    from dataclasses import replace

    delta = replace(_delta(), output_tokens=48)  # main model only
    span = turn_span_from_claude_result(_result(), delta, end_ts=BASE)
    assert span["gen_ai.usage.output_tokens"] == 2 + 48
    ledger_out = sum(m.output_tokens for m in delta.models)
    assert span["gen_ai.usage.output_tokens"] == ledger_out


def test_turn_span_output_tokens_second_turn_of_a_session_matches_ledger() -> None:
    """#2684 slice 4, as measured on gongmyoung 2026-11-07: ``model_usage`` is
    session-cumulative but the top-level ``usage`` is not, so the snapshot
    delta of the top-level output went to 0 on later turns of a session while
    the per-model delta (what the cost ledger records) stayed correct."""

    from telegram_bot.core.usage import delta_from_snapshots, parse_claude_result

    def sdk(top_out: int, opus_out: int, haiku_out: int, cost: float):
        return SimpleNamespace(
            usage={"input_tokens": 5, "output_tokens": top_out},
            model_usage={
                "claude-opus-5-5": {"inputTokens": 50, "outputTokens": opus_out, "costUSD": cost},
                "claude-haiku-5-5": {"inputTokens": 9, "outputTokens": haiku_out, "costUSD": 0.001},
            },
            total_cost_usd=cost + 0.001,
        )

    first = parse_claude_result(sdk(top_out=4609, opus_out=4609, haiku_out=215, cost=0.8))
    second = parse_claude_result(sdk(top_out=481, opus_out=4609 + 2788, haiku_out=215, cost=1.0))
    delta = delta_from_snapshots(first, second)
    span = turn_span_from_claude_result(_result(), delta, end_ts=BASE)
    ledger_out = sum(m.output_tokens for m in delta.models)
    assert ledger_out == 2788
    assert span["gen_ai.usage.output_tokens"] == ledger_out


def test_turn_span_output_tokens_fall_back_to_top_level_without_models() -> None:
    delta = UsageSnapshot(provider="claude", input_tokens=7, output_tokens=11, total_cost_usd=0.1)
    span = turn_span_from_claude_result(_result(), delta, end_ts=BASE)
    assert span["gen_ai.usage.output_tokens"] == 11
    assert span["gen_ai.usage.input_tokens"] == 7


def test_turn_span_from_error_result() -> None:
    span = turn_span_from_claude_result(
        _result(is_error=True, subtype="error_max_turns"), None, end_ts=BASE
    )
    clean = sanitize_span(span)
    assert clean is not None
    assert clean["status"] == "error"
    assert clean["error.type"] == "error_max_turns"
    assert clean["ccc.outcome"] == "error_max_turns"
    assert "gen_ai.request.model" not in clean


# ---- call site: ProjectChatHandler.record_claude_result_snapshot -------------


def _sdk_result(**overrides):
    message = SimpleNamespace(
        subtype="success",
        duration_ms=1500,
        duration_api_ms=1400,
        is_error=False,
        num_turns=1,
        session_id="claude-a",
        uuid="11111111-2222-3333-4444-555555555555",
        result="private answer text",
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={
            "claude-opus-4-5": {"inputTokens": 100, "outputTokens": 20, "costUSD": 0.01}
        },
        total_cost_usd=0.01,
    )
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _bare_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from telegram_bot.core.project_chat import ProjectChatHandler

    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._usage_meter = None
    handler._cost_ledger = None
    handler._clock = SimpleNamespace(time=lambda: BASE)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    return handler


class _FakeSpans:
    def __init__(self, error: Exception | None = None) -> None:
        self.spans: list[dict] = []
        self.error = error

    def record(self, span):
        if self.error is not None:
            raise self.error
        self.spans.append(dict(span))
        return True


def test_turn_call_site_emits_one_turn_span_per_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _bare_handler(tmp_path, monkeypatch)
    fake = _FakeSpans()
    handler._span_ledger = fake

    handler.record_claude_result_snapshot(7, 9, _sdk_result())
    # Session-cumulative totals: the second span must carry only the delta.
    handler.record_claude_result_snapshot(
        7,
        9,
        _sdk_result(
            uuid="66666666-7777-8888-9999-000000000000",
            usage={"input_tokens": 150, "output_tokens": 35},
            model_usage={
                "claude-opus-4-5": {"inputTokens": 150, "outputTokens": 35, "costUSD": 0.03}
            },
            total_cost_usd=0.03,
        ),
    )

    assert len(fake.spans) == 2
    first = sanitize_span(fake.spans[0])
    second = sanitize_span(fake.spans[1])
    assert first is not None and second is not None
    assert first["kind"] == "turn" and first["status"] == "ok"
    assert first["gen_ai.request.model"] == "claude-opus-4-5"
    assert first["gen_ai.usage.output_tokens"] == 20
    assert first["ccc.turn_id"] == "11111111-2222-3333-4444-555555555555"
    assert first["duration_ms"] == 1500
    assert second["gen_ai.usage.output_tokens"] == 15
    assert second["ccc.cost_usd"] == pytest.approx(0.02)
    assert "private answer text" not in json.dumps(fake.spans)


def test_turn_call_site_swallows_span_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _bare_handler(tmp_path, monkeypatch)
    handler._span_ledger = _FakeSpans(error=RuntimeError("span ledger exploded"))

    handler.record_claude_result_snapshot(7, 9, _sdk_result())  # must not raise

    # The /usage cache the turn path depends on is still updated.
    assert (7, 9, "claude-a") in handler._claude_usage


def test_turn_call_site_without_span_ledger_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _bare_handler(tmp_path, monkeypatch)  # no _span_ledger attribute
    handler.record_claude_result_snapshot(7, 9, _sdk_result())
    assert (7, 9, "claude-a") in handler._claude_usage


def _wired_settings(project_root: Path, **overrides) -> SimpleNamespace:
    settings = SimpleNamespace(
        agent_provider="claude",
        project_root=project_root,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        enable_streaming=False,
        enable_partial_streaming=False,
        usage_meter_enabled=True,
        usage_budget_tokens_claude=0,
        usage_budget_tokens_codex=0,
        usage_budget_tokens_piri=0,
        usage_budget_warn_percent=80,
        push_enabled=False,
        push_spool_dir=str(project_root / "spool"),
        channel="matrix",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def test_handler_wires_a_real_span_ledger_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core.project_chat import ProjectChatHandler

    monkeypatch.setenv("CCC_NODE", "spannode")
    handler = ProjectChatHandler(settings=_wired_settings(tmp_path, span_retention_days=3))
    ledger = handler._span_ledger
    assert isinstance(ledger, SpanLedger)
    assert ledger.root == tmp_path.resolve() / ".telegram_bot" / "spans"
    assert ledger.retention_days == 3

    handler.record_claude_result_snapshot(7, 9, _sdk_result())

    files = list(ledger.root.glob("*.jsonl"))
    assert len(files) == 1
    (span,) = list(iter_spans(files[0]))
    assert span["ccc.node"] == "spannode"
    assert span["ccc.channel"] == "matrix"
    assert span["gen_ai.conversation.id"] == conversation_ref("claude-a")


@pytest.mark.parametrize(
    "overrides",
    [{"span_ledger_enabled": False}, {"usage_meter_enabled": False}],
)
def test_span_ledger_kill_switches(tmp_path: Path, overrides: dict) -> None:
    from telegram_bot.core.project_chat import ProjectChatHandler

    handler = ProjectChatHandler(settings=_wired_settings(tmp_path, **overrides))
    assert handler._span_ledger is None
    handler.record_claude_result_snapshot(7, 9, _sdk_result())
    assert not (tmp_path / ".telegram_bot" / "spans").exists()


def test_settings_expose_span_env_knobs() -> None:
    from telegram_bot.utils.config import Config

    fields = Config.model_fields
    assert fields["span_ledger_enabled"].alias == "CCC_SPAN_LEDGER_ENABLED"
    assert fields["span_ledger_enabled"].default is True
    assert fields["span_retention_days"].alias == "CCC_SPAN_RETENTION_DAYS"
    assert fields["span_retention_days"].default == DEFAULT_RETENTION_DAYS
