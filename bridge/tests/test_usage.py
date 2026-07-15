"""Provider-neutral usage parsing, rendering, and secure cache tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.usage import (
    MAX_TELEGRAM_USAGE_LENGTH,
    UsageSnapshot,
    UsageWindow,
    load_claude_status_snapshot,
    merge_usage,
    parse_claude_result,
    parse_codex_account_usage,
    parse_codex_rate_limits,
    parse_codex_thread_usage,
    render_usage,
    status_snapshot_name,
)


COLLECTOR = Path(__file__).resolve().parents[2] / "claude" / "hooks" / "statusline-usage.py"


def test_codex_protocol_parsing_keeps_multiple_buckets_deterministic() -> None:
    parsed = parse_codex_rate_limits(
        {
            "rateLimits": {"planType": "plus"},
            "rateLimitsByLimitId": {
                "weekly": {
                    "limitName": "Weekly",
                    "primary": {
                        "usedPercent": 25.5,
                        "windowDurationMins": 300,
                        "resetsAt": 1_900_000_000,
                    },
                },
                "five-hour": {
                    "limitName": "Five hour",
                    "primary": {
                        "usedPercent": 25.5,
                        "windowDurationMins": 300,
                        "resetsAt": 1_900_000_000,
                    },
                    "secondary": {"usedPercent": float("nan")},
                },
            },
            "rateLimitResetCredits": {"opaqueId": "must-not-be-read"},
        }
    )

    assert parsed.plan_type == "plus"
    assert [window.label for window in parsed.windows] == [
        "Five hour primary",
        "Weekly primary",
    ]
    assert [window.used_percent for window in parsed.windows] == [25.5, 25.5]


def test_codex_account_and_exact_thread_usage_are_bounded_and_defensive() -> None:
    account = parse_codex_account_usage(
        {
            "summary": {"lifetimeTokens": 1234},
            "dailyUsageBuckets": [
                {"startDate": "2026-07-14", "tokens": 20},
                {"startDate": "2026-07-15", "tokens": 30},
                {"startDate": "bad", "tokens": -1},
            ],
        }
    )
    thread = parse_codex_thread_usage(
        {
            "tokenUsage": {
                "last": {"totalTokens": 500},
                "total": {
                    "inputTokens": 1000,
                    "outputTokens": 200,
                    "totalTokens": 1200,
                },
                "modelContextWindow": 200_000,
            }
        }
    )
    merged = merge_usage(account, thread)

    assert merged.lifetime_tokens == 1234
    assert [item.date for item in merged.daily_usage] == ["2026-07-15", "2026-07-14"]
    assert merged.context_used == 500
    assert merged.context_window == 200_000
    assert merged.total_tokens == 1200


def test_sparse_rate_limit_updates_merge_by_bucket() -> None:
    old = UsageSnapshot(
        provider="codex",
        windows=(UsageWindow("five hour", 10), UsageWindow("weekly", 20)),
    )
    sparse = UsageSnapshot(provider="codex", windows=(UsageWindow("five hour", 35),))

    merged = merge_usage(old, sparse)

    assert [(item.label, item.used_percent) for item in merged.windows] == [
        ("five hour", 35),
        ("weekly", 20),
    ]


def test_claude_sdk_result_parser_uses_only_numeric_usage_fields() -> None:
    parsed = parse_claude_result(
        SimpleNamespace(
            usage={
                "input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "output_tokens": 40,
                "account_id": "must-not-appear",
            },
            model_usage={"secret-model-name": {"contextWindow": 200_000}},
            total_cost_usd=0.125,
        ),
        observed_at=1000,
    )

    assert parsed.context_used == 150
    assert parsed.context_window == 200_000
    assert parsed.total_tokens == 190
    assert parsed.total_cost_usd == 0.125

    fallback = parse_claude_result(
        SimpleNamespace(
            usage=None,
            model_usage={
                "model-a": {
                    "inputTokens": 10,
                    "cacheReadInputTokens": 5,
                    "outputTokens": 2,
                    "contextWindow": 100_000,
                },
                "model-b": {"inputTokens": 20, "outputTokens": 3},
            },
            total_cost_usd=None,
        ),
        observed_at=1000,
    )
    assert fallback.input_tokens == 30
    assert fallback.context_used == 35
    assert fallback.output_tokens == 5


def _run_collector(tmp_path: Path, payload: object, *, state_dir: Path | None = None) -> None:
    env = {
        **os.environ,
        "CCC_STATE_DIR": str(state_dir or (tmp_path / "state")),
    }
    subprocess.run(
        [sys.executable, str(COLLECTOR)],
        input=json.dumps(payload).encode(),
        env=env,
        check=True,
        timeout=5,
    )


def test_optional_statusline_collector_writes_allowlisted_owner_only_snapshot(
    tmp_path: Path,
) -> None:
    _run_collector(
        tmp_path,
        {
            "session_id": "session-a",
            "transcript_path": "/secret/transcript.jsonl",
            "model": {"id": "secret-model"},
            "context_window": {
                "total_input_tokens": 500,
                "total_output_tokens": 50,
                "context_window_size": 200_000,
            },
            "cost": {"total_cost_usd": 0.25},
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 2_000_000_000},
                "seven_day": {"used_percentage": 41.2, "resets_at": 2_000_100_000},
            },
        },
    )
    path = tmp_path / "state" / "usage" / status_snapshot_name("session-a")
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text()
    assert "transcript" not in raw
    assert "secret-model" not in raw
    parsed = load_claude_status_snapshot(path.parent, "session-a", now=path.stat().st_mtime + 1)
    assert parsed is not None
    assert parsed.context_used == 500
    assert parsed.context_window == 200_000
    assert parsed.output_tokens == 50
    assert parsed.total_tokens == 550
    assert [window.label for window in parsed.windows] == ["five hour", "seven day"]


def test_status_snapshot_rejects_stale_wrong_session_and_symlink(tmp_path: Path) -> None:
    usage = tmp_path / "state" / "usage"
    usage.mkdir(parents=True, mode=0o700)
    stale = usage / status_snapshot_name("session-a")
    stale.write_text(json.dumps({"observedAt": 1, "context": {"usedTokens": 5}}))
    stale.chmod(0o600)
    assert load_claude_status_snapshot(usage, "session-a", now=10_000) is None
    assert load_claude_status_snapshot(usage, "session-b", now=1) is None

    target = tmp_path / "target.json"
    target.write_text("unchanged")
    stale.unlink()
    stale.symlink_to(target)
    assert load_claude_status_snapshot(usage, "session-a", now=1) is None
    _run_collector(
        tmp_path, {"session_id": "session-a", "context_window": {"total_input_tokens": 9}}
    )
    assert stale.is_symlink()
    assert target.read_text() == "unchanged"

    linked_directory = tmp_path / "linked-usage"
    linked_directory.symlink_to(usage, target_is_directory=True)
    assert load_claude_status_snapshot(linked_directory, "session-a", now=1) is None

    hardlink_directory = tmp_path / "hardlink-usage"
    hardlink_directory.mkdir(mode=0o700)
    hardlink_target = tmp_path / "hardlink-target.json"
    hardlink_target.write_text(json.dumps({"observedAt": 1}))
    hardlink_target.chmod(0o600)
    os.link(
        hardlink_target,
        hardlink_directory / status_snapshot_name("hardlinked-session"),
    )
    assert load_claude_status_snapshot(hardlink_directory, "hardlinked-session", now=1) is None

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    real_usage = real_parent / "usage"
    real_usage.mkdir(mode=0o700)
    real_snapshot = real_usage / status_snapshot_name("intermediate-session")
    real_snapshot.write_text(json.dumps({"observedAt": 1, "context": {"usedTokens": 3}}))
    real_snapshot.chmod(0o600)
    assert load_claude_status_snapshot(real_usage, "intermediate-session", now=1) is not None
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    assert (
        load_claude_status_snapshot(linked_parent / "usage", "intermediate-session", now=1) is None
    )

    real_state = tmp_path / "real-state"
    real_state.mkdir(mode=0o700)
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(real_state, target_is_directory=True)
    _run_collector(
        tmp_path,
        {"session_id": "linked-write", "context_window": {"total_input_tokens": 1}},
        state_dir=linked_state,
    )
    assert not (real_state / "usage").exists()


def test_collector_prunes_expired_snapshots(tmp_path: Path) -> None:
    _run_collector(
        tmp_path,
        {"session_id": "old-session", "context_window": {"total_input_tokens": 1}},
    )
    old = tmp_path / "state" / "usage" / status_snapshot_name("old-session")
    os.utime(old, (1, 1))

    _run_collector(
        tmp_path,
        {"session_id": "new-session", "context_window": {"total_input_tokens": 2}},
    )

    assert not old.exists()


def test_renderer_marks_unavailable_and_is_telegram_bounded() -> None:
    assert "Rate limits: unavailable" in render_usage(UsageSnapshot(provider="claude"))
    huge = UsageSnapshot(
        provider="codex",
        windows=tuple(UsageWindow("x" * 80 + str(index), index % 100) for index in range(16)),
        daily_usage=(),
    )
    assert len(render_usage(huge)) <= MAX_TELEGRAM_USAGE_LENGTH


@pytest.mark.anyio
async def test_claude_usage_cache_is_exact_conversation_and_session_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    message = SimpleNamespace(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="claude-a",
        result="ok",
        usage={"input_tokens": 100, "output_tokens": 20},
        total_cost_usd=0.01,
    )
    handler._record_claude_usage(SimpleNamespace(user_id=7, chat_id=9), message)

    exact = await handler.get_usage(7, 9, "claude-a")
    other_chat = await handler.get_usage(7, 10, "claude-a")
    other_session = await handler.get_usage(7, 9, "claude-b")

    assert exact.context_used == 100
    assert exact.total_cost_usd == 0.01
    assert other_chat.context_used is None
    assert other_session.context_used is None
