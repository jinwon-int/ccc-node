"""Provider-neutral usage parsing, rendering, and secure cache tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.usage import (
    MAX_RAW_WINDOWS,
    MAX_TELEGRAM_USAGE_LENGTH,
    DailyUsage,
    ModelUsage,
    UsageSnapshot,
    UsageWindow,
    detect_claude_service,
    load_claude_status_snapshot,
    local_claude_environment_snapshot,
    merge_usage,
    parse_claude_rate_limit_event,
    parse_claude_result,
    parse_codex_account_usage,
    parse_codex_rate_limits,
    parse_codex_thread_usage,
    render_usage,
    status_snapshot_name,
    synthesize_service_windows,
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
    # No numeric token/cost data for the model, so its id must not surface.
    assert parsed.models == ()

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


def test_claude_result_parser_builds_per_model_map_in_both_casings() -> None:
    parsed = parse_claude_result(
        SimpleNamespace(
            usage={"input_tokens": 100, "output_tokens": 40},
            model_usage={
                "claude-fable-5": {
                    "inputTokens": 90,
                    "cacheCreationInputTokens": 20,
                    "cacheReadInputTokens": 30,
                    "outputTokens": 35,
                    "costUSD": 0.2,
                    "contextWindow": 200_000,
                },
                "claude-haiku-4-5": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 5,
                    "output_tokens": 5,
                    "cost_usd": 0.01,
                },
                "identifier-only-model": {"contextWindow": 100_000},
                "account_id": "must-not-appear",
            },
            total_cost_usd=0.21,
        ),
        observed_at=1000,
    )

    assert parsed.models == (
        ModelUsage(
            model="claude-fable-5",
            input_tokens=90,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=30,
            output_tokens=35,
            cost_usd=0.2,
        ),
        ModelUsage(
            model="claude-haiku-4-5",
            input_tokens=10,
            cache_read_input_tokens=5,
            output_tokens=5,
            cost_usd=0.01,
        ),
    )
    # Identifier-only entries (no numeric token/cost data) never surface.
    assert all(entry.model != "identifier-only-model" for entry in parsed.models)


def test_merge_usage_sums_per_model_totals_across_snapshots() -> None:
    first = UsageSnapshot(
        provider="claude",
        models=(
            ModelUsage(
                model="claude-fable-5",
                input_tokens=100,
                cache_read_input_tokens=50,
                output_tokens=10,
                cost_usd=0.1,
            ),
        ),
    )
    second = UsageSnapshot(
        provider="claude",
        models=(
            ModelUsage(model="claude-fable-5", input_tokens=20, output_tokens=5),
            ModelUsage(model="claude-haiku-4-5", input_tokens=7, output_tokens=3, cost_usd=0.01),
        ),
    )

    merged = merge_usage(first, second)

    assert merged.models == (
        ModelUsage(
            model="claude-fable-5",
            input_tokens=120,
            cache_read_input_tokens=50,
            output_tokens=15,
            cost_usd=0.1,
        ),
        ModelUsage(model="claude-haiku-4-5", input_tokens=7, output_tokens=3, cost_usd=0.01),
    )
    # A snapshot without a per-model map keeps the accumulated one intact.
    assert merge_usage(merged, UsageSnapshot(provider="claude")).models == merged.models


def test_renderer_lists_per_model_usage_only_when_present() -> None:
    rendered = render_usage(
        UsageSnapshot(
            provider="claude",
            models=(
                ModelUsage(
                    model="claude-fable-5",
                    input_tokens=1_234_000,
                    cache_creation_input_tokens=500,
                    cache_read_input_tokens=67,
                    output_tokens=89_012,
                    cost_usd=0.1234,
                ),
                ModelUsage(model="claude-haiku-4-5", input_tokens=100, output_tokens=10),
            ),
        )
    )

    assert "Models:" in rendered
    assert "  claude-fable-5 · in 1,234,567 · out 89,012 · $0.1234" in rendered
    # Cost is omitted when the SDK did not report one for the model.
    assert "  claude-haiku-4-5 · in 100 · out 10" in rendered
    assert "claude-haiku-4-5 · in 100 · out 10 · $" not in rendered

    assert "Models:" not in render_usage(UsageSnapshot(provider="claude"))


def test_claude_rate_limit_event_parser_converts_utilization_and_ignores_incomplete() -> None:
    parsed = parse_claude_rate_limit_event(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed_warning",
                rate_limit_type="five_hour",
                utilization=0.812,
                resets_at=1_900_000_000,
            )
        ),
        observed_at=1000,
    )
    assert [w.label for w in parsed.windows] == ["five hour"]
    assert parsed.windows[0].used_percent == pytest.approx(81.2)
    assert parsed.windows[0].resets_at == 1_900_000_000

    # Successive events (five_hour, then seven_day) accumulate via merge_usage
    # instead of clobbering each other — each SDK event only reports one window.
    second = parse_claude_rate_limit_event(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed",
                rate_limit_type="seven_day",
                utilization=0.1,
                resets_at=None,
            )
        ),
        observed_at=1001,
    )
    merged = merge_usage(parsed, second)
    assert [w.label for w in merged.windows] == ["five hour", "seven day"]

    # Missing type/utilization (e.g. an overage-only event) yields no window
    # rather than a bogus zero-percent entry.
    incomplete = parse_claude_rate_limit_event(
        SimpleNamespace(rate_limit_info=SimpleNamespace(status="rejected")),
        observed_at=1002,
    )
    assert incomplete.windows == ()


def test_concurrent_rate_limit_buckets_each_render_and_survive_merge() -> None:
    """Distinct rate_limit_type buckets (e.g. per-model-class weekly windows)
    must accumulate as independent lines, never last-write-wins over one slot."""

    events = [
        parse_claude_rate_limit_event(
            SimpleNamespace(
                rate_limit_info=SimpleNamespace(
                    status="allowed",
                    rate_limit_type=limit_type,
                    utilization=utilization,
                    resets_at=1_900_000_000,
                )
            ),
            observed_at=1000 + index,
        )
        for index, (limit_type, utilization) in enumerate(
            [("five_hour", 0.4), ("seven_day_opus", 0.7), ("seven_day_sonnet", 0.2)]
        )
    ]

    merged = merge_usage(*events)

    assert [(w.label, w.used_percent) for w in merged.windows] == [
        ("five hour", pytest.approx(40.0)),
        ("seven day opus", pytest.approx(70.0)),
        ("seven day sonnet", pytest.approx(20.0)),
    ]
    rendered = render_usage(merged)
    assert "- five hour: 40% used" in rendered
    assert "- seven day opus: 70% used" in rendered
    assert "- seven day sonnet: 20% used" in rendered


def test_overage_state_is_parsed_allowlisted_merged_and_rendered() -> None:
    parsed = parse_claude_rate_limit_event(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed_warning",
                rate_limit_type="five_hour",
                utilization=0.9,
                resets_at=1_900_000_000,
                overage_status="allowed",
                overage_resets_at=1_900_100_000,
            )
        ),
        observed_at=1000,
    )
    assert parsed.overage_status == "allowed"
    assert parsed.overage_resets_at == 1_900_100_000

    # Overage state survives merging with later window-only events.
    window_only = parse_claude_rate_limit_event(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed",
                rate_limit_type="seven_day",
                utilization=0.1,
                resets_at=None,
            )
        ),
        observed_at=1001,
    )
    merged = merge_usage(parsed, window_only)
    assert merged.overage_status == "allowed"
    assert merged.overage_resets_at == 1_900_100_000

    rendered = render_usage(merged)
    assert "Overage: allowed · " in rendered
    assert "Overage" not in render_usage(window_only)

    # Undocumented status strings are dropped, never rendered.
    unknown = parse_claude_rate_limit_event(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed",
                overage_status="secret-account-state",
                overage_resets_at=1_900_100_000,
            )
        ),
        observed_at=1002,
    )
    assert unknown.overage_status is None
    assert unknown.overage_resets_at is None


def _rate_limit_event(raw: object) -> SimpleNamespace:
    """Build a scripted RateLimitEvent-shaped message with a raw passthrough."""

    return SimpleNamespace(
        rate_limit_info=SimpleNamespace(
            status="allowed",
            rate_limit_type="five_hour",
            utilization=0.5,
            resets_at=1_900_000_000,
            raw=raw,
        )
    )


def test_raw_windows_container_adds_model_class_buckets_and_renders() -> None:
    """A per-window map under a container key in info.raw yields extra buckets
    (mixed resetsAt/resets_at spellings accepted) alongside the primary."""

    parsed = parse_claude_rate_limit_event(
        _rate_limit_event(
            {
                "windows": {
                    "five_hour": {"utilization": 0.5, "resetsAt": 1_900_000_000},
                    "seven_day_opus": {"utilization": 0.12, "resets_at": 1_900_200_000},
                }
            }
        ),
        observed_at=1000,
    )
    assert [(w.label, w.used_percent) for w in parsed.windows] == [
        ("five hour", pytest.approx(50.0)),
        ("seven day opus", pytest.approx(12.0)),
    ]
    assert parsed.windows[1].resets_at == 1_900_200_000
    rendered = render_usage(parsed)
    assert "- five hour: 50% used" in rendered
    assert "- seven day opus: 12% used" in rendered


def test_raw_top_level_window_shaped_keys_are_accepted() -> None:
    """Window-shaped top-level raw entries count too; non-window siblings
    (status strings, scalars) are ignored rather than misparsed."""

    parsed = parse_claude_rate_limit_event(
        _rate_limit_event(
            {
                "status": "allowed",
                "utilization": 0.5,
                "resetsAt": 1_900_000_000,
                "seven_day_opus": {"utilization": 0.12, "resetsAt": 1_900_200_000},
            }
        ),
        observed_at=1000,
    )
    assert [w.label for w in parsed.windows] == ["five hour", "seven day opus"]


def test_raw_window_map_malformed_entries_are_dropped() -> None:
    parsed = parse_claude_rate_limit_event(
        _rate_limit_event(
            {
                "windows": {
                    "seven_day_opus": {"utilization": 0.12, "resetsAt": 1_900_200_000},
                    "over_range": {"utilization": 1.5, "resetsAt": 1_900_200_000},
                    "negative": {"utilization": -0.1, "resetsAt": 1_900_200_000},
                    "boolean": {"utilization": True, "resetsAt": 1_900_200_000},
                    "non_numeric": {"utilization": "0.5", "resetsAt": 1_900_200_000},
                    "missing_reset": {"utilization": 0.4},
                    "reset_absurd": {"utilization": 0.4, "resetsAt": 10**12},
                    "not_a_dict": 0.4,
                    "Absurd-Key!": {"utilization": 0.4, "resetsAt": 1_900_200_000},
                    "x" * 65: {"utilization": 0.4, "resetsAt": 1_900_200_000},
                }
            }
        ),
        observed_at=1000,
    )
    assert [w.label for w in parsed.windows] == ["five hour", "seven day opus"]

    # A raw payload with no window map at all keeps today's single-window shape.
    plain = parse_claude_rate_limit_event(_rate_limit_event({}), observed_at=1000)
    assert [w.label for w in plain.windows] == ["five hour"]
    no_raw = parse_claude_rate_limit_event(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed",
                rate_limit_type="five_hour",
                utilization=0.5,
                resets_at=1_900_000_000,
            )
        ),
        observed_at=1000,
    )
    assert parsed.windows[0] == plain.windows[0] == no_raw.windows[0]


def test_raw_window_map_never_overrides_the_primary_window() -> None:
    parsed = parse_claude_rate_limit_event(
        _rate_limit_event(
            {
                "windows": {
                    "five_hour": {"utilization": 0.99, "resetsAt": 1_900_999_999},
                }
            }
        ),
        observed_at=1000,
    )
    assert [(w.label, w.used_percent, w.resets_at) for w in parsed.windows] == [
        ("five hour", pytest.approx(50.0), 1_900_000_000)
    ]


def test_raw_window_map_is_capped_at_eight_windows() -> None:
    assert MAX_RAW_WINDOWS == 8
    parsed = parse_claude_rate_limit_event(
        _rate_limit_event(
            {
                "windows": {
                    f"window_{index:02d}": {
                        "utilization": 0.1,
                        "resetsAt": 1_900_000_000,
                    }
                    for index in range(10)
                }
            }
        ),
        observed_at=1000,
    )
    assert len(parsed.windows) == 1 + MAX_RAW_WINDOWS
    assert [w.label for w in parsed.windows] == ["five hour"] + [
        f"window {index:02d}" for index in range(MAX_RAW_WINDOWS)
    ]


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
    claude = render_usage(UsageSnapshot(provider="claude"))
    assert "Rate limits: unavailable" in claude
    assert "Context: unavailable" in claude
    assert "Session tokens: input unavailable" in claude
    huge = UsageSnapshot(
        provider="codex",
        windows=tuple(UsageWindow("x" * 80 + str(index), index % 100) for index in range(16)),
        daily_usage=(),
    )
    assert len(render_usage(huge)) <= MAX_TELEGRAM_USAGE_LENGTH


def test_codex_renderer_hides_spark_account_history_and_empty_session_lines() -> None:
    reset = datetime(2026, 7, 16, tzinfo=timezone.utc).timestamp()
    rendered = render_usage(
        UsageSnapshot(
            provider="codex",
            plan_type="plus",
            windows=(
                UsageWindow("GPT-5.3-Codex-Spark primary", 12, resets_at=reset),
                UsageWindow("Five hour primary", 25, resets_at=reset),
            ),
            lifetime_tokens=2_861_652_645,
            daily_usage=(DailyUsage("2026-02-06", 58_913_824),),
        )
    )

    assert "GPT-5.3-Codex-Spark" not in rendered
    assert "Five hour primary" in rendered
    assert "2026-07-16 09:00 KST" in rendered
    assert "Context:" not in rendered
    assert "Session tokens:" not in rendered
    assert "Account lifetime tokens:" not in rendered
    assert "Daily:" not in rendered


def test_codex_renderer_keeps_available_context_and_session_tokens() -> None:
    rendered = render_usage(
        UsageSnapshot(
            provider="codex",
            windows=(UsageWindow("GPT-5.3-Codex-Spark primary", 12),),
            context_used=500,
            context_window=2_000,
            input_tokens=1_000,
            output_tokens=200,
            total_tokens=1_200,
        )
    )

    assert "Rate limits:" not in rendered
    assert "Context: 500 / 2,000 (25.0%)" in rendered
    assert "Session tokens: input 1,000 · output 200 · total 1,200" in rendered


@pytest.mark.anyio
async def test_claude_usage_cache_is_exact_conversation_and_session_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._usage_meter = None
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
    handler.record_claude_result_snapshot(7, 9, message)

    exact = await handler.get_usage(7, 9, "claude-a")
    other_chat = await handler.get_usage(7, 10, "claude-a")
    other_session = await handler.get_usage(7, 9, "claude-b")

    assert exact.context_used == 100
    assert exact.total_cost_usd == 0.01
    assert other_chat.context_used is None
    assert other_session.context_used is None


@pytest.mark.anyio
async def test_claude_rate_limit_is_global_unlike_session_scoped_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rate limits belong to the one Claude credential this node runs as, not
    to any single Telegram conversation — so they must surface for every
    chat/session's /usage call, unlike the token/cost cache asserted scoped
    above."""

    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._claude_rate_limit = None
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")

    handler._record_claude_rate_limit(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="allowed_warning",
                rate_limit_type="five_hour",
                utilization=0.5,
                resets_at=1_900_000_000,
            ),
            session_id="claude-a",
        )
    )
    # Empty-window events (e.g. status-only payloads) must not wipe out the
    # previously observed real window.
    handler._record_claude_rate_limit(
        SimpleNamespace(rate_limit_info=SimpleNamespace(status="rejected"), session_id="claude-a")
    )
    # Overage-only events carry no window but must still record overage state.
    handler._record_claude_rate_limit(
        SimpleNamespace(
            rate_limit_info=SimpleNamespace(
                status="rejected",
                overage_status="allowed_warning",
                overage_resets_at=1_900_100_000,
            ),
            session_id="claude-a",
        )
    )

    for user_id, chat_id, session_id in ((7, 9, "claude-a"), (7, 10, "claude-a"), (99, 1, "unrelated")):
        result = await handler.get_usage(user_id, chat_id, session_id)
        assert [w.label for w in result.windows] == ["five hour"]
        assert result.windows[0].used_percent == 50.0
        assert result.overage_status == "allowed warning"
        assert result.overage_resets_at == 1_900_100_000


@pytest.mark.anyio
async def test_get_usage_tolerates_handler_without_rate_limit_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards test/legacy fixtures that build the handler via __new__ and
    never set `_claude_rate_limit` (added after `_claude_usage`)."""

    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._usage_meter = None
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    assert not hasattr(handler, "_claude_rate_limit")

    result = await handler.get_usage(1, 2, "claude-x")
    assert result.windows == ()


_SERVICE_ENV_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
)


def _clear_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SERVICE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_detect_claude_service_matches_kimi_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    assert detect_claude_service() == "Kimi Code"
    assert detect_claude_service("https://api.kimi.com/coding/") == "Kimi Code"
    assert detect_claude_service("api.kimi.com/coding/") == "Kimi Code"
    assert detect_claude_service("https://KIMI.COM/coding/") == "Kimi Code"


def test_detect_claude_service_ignores_anthropic_and_lookalikes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_env(monkeypatch)
    assert detect_claude_service() is None
    assert detect_claude_service(None) is None
    assert detect_claude_service("") is None
    assert detect_claude_service("https://api.anthropic.com") is None
    assert detect_claude_service("https://notkimi.com/coding/") is None
    assert detect_claude_service("https://kimi.com.evil.example") is None
    assert detect_claude_service("not a url at all:://") is None


def test_local_claude_environment_snapshot_reads_kimi_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    monkeypatch.setenv("ANTHROPIC_MODEL", "k3[1m]")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1048576")
    snapshot = local_claude_environment_snapshot()
    assert snapshot.provider == "claude"
    assert snapshot.service == "Kimi Code"
    assert snapshot.plan_type == "Kimi Code · k3[1m] · effort max"
    assert snapshot.context_window == 1048576


def test_local_claude_environment_snapshot_rejects_bad_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "10x; rm -rf /")
    assert local_claude_environment_snapshot().context_window is None
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", str(10**12))
    assert local_claude_environment_snapshot().context_window is None


def test_local_claude_environment_snapshot_default_env_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_service_env(monkeypatch)
    assert local_claude_environment_snapshot() == UsageSnapshot(provider="claude")


def test_merge_usage_observed_values_override_environment_base() -> None:
    base = UsageSnapshot(
        provider="claude",
        service="Kimi Code",
        plan_type="Kimi Code · k3[1m]",
        context_window=1048576,
    )
    observed = UsageSnapshot(provider="claude", context_used=123, context_window=262144)
    merged = merge_usage(base, observed)
    assert merged.service == "Kimi Code"
    assert merged.plan_type == "Kimi Code · k3[1m]"
    assert merged.context_used == 123
    assert merged.context_window == 262144


def test_render_usage_shows_kimi_service_title_and_quota_note() -> None:
    snapshot = UsageSnapshot(
        provider="claude",
        service="Kimi Code",
        plan_type="Kimi Code · k3[1m] · effort max",
        context_used=1000,
        context_window=1048576,
    )
    rendered = render_usage(snapshot)
    assert rendered.splitlines()[0] == "📊 Usage · Kimi Code"
    assert "Plan: Kimi Code · k3[1m] · effort max" in rendered
    assert "Kimi quota: rolling 5-hour + weekly windows apply" in rendered
    assert "Context: 1,000 / 1,048,576" in rendered


def test_render_usage_without_service_is_unchanged() -> None:
    rendered = render_usage(UsageSnapshot(provider="claude"))
    assert rendered.splitlines()[0] == "📊 Usage · Claude Code"
    assert "Kimi quota" not in rendered


@pytest.mark.anyio
async def test_get_usage_bases_snapshot_on_kimi_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    monkeypatch.setenv("ANTHROPIC_MODEL", "k3[1m]")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1048576")
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._usage_meter = None
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")

    result = await handler.get_usage(1, 2, None)
    assert result.service == "Kimi Code"
    assert result.plan_type == "Kimi Code · k3[1m] · effort max"
    assert result.context_window == 1048576
    assert result.windows == ()


def test_synthesize_service_windows_builds_count_only_kimi_window() -> None:
    windows = synthesize_service_windows(
        "Kimi Code", {"requests": 47, "tokens": 29_000_000}
    )
    assert len(windows) == 1
    window = windows[0]
    assert window.label == "Kimi 5-hour"
    assert window.used_percent is None
    assert window.used_count == 47
    assert window.count_unit == "req"


def test_synthesize_service_windows_rejects_missing_or_bad_inputs() -> None:
    assert synthesize_service_windows(None, {"requests": 1}) == ()
    assert synthesize_service_windows("Kimi Code", None) == ()
    assert synthesize_service_windows("Kimi Code", {}) == ()
    assert synthesize_service_windows("Unknown Service", {"requests": 1}) == ()
    assert synthesize_service_windows("Kimi Code", {"requests": -1}) == ()
    assert synthesize_service_windows("Kimi Code", {"requests": True}) == ()
    assert synthesize_service_windows("Kimi Code", {"requests": "47"}) == ()


def test_render_usage_count_only_window_skips_percent_and_reset() -> None:
    snapshot = UsageSnapshot(
        provider="claude",
        service="Kimi Code",
        windows=(
            UsageWindow(
                label="Kimi 5-hour",
                used_percent=None,
                used_count=47,
                count_unit="req",
            ),
        ),
    )
    rendered = render_usage(snapshot)
    assert "- Kimi 5-hour: 47 req used (local estimate)" in rendered
    assert "reset unavailable" not in rendered
    assert "% used" not in rendered


def test_render_usage_count_only_window_handles_missing_count() -> None:
    snapshot = UsageSnapshot(
        provider="claude",
        windows=(UsageWindow(label="Kimi 5-hour", used_percent=None),),
    )
    rendered = render_usage(snapshot)
    assert "- Kimi 5-hour: unavailable used (local estimate)" in rendered


@pytest.mark.anyio
async def test_get_usage_synthesizes_kimi_window_from_meter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    monkeypatch.setenv("ANTHROPIC_MODEL", "k3[1m]")
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    handler._usage_meter = SimpleNamespace(
        rolling_usage=lambda: {"claude": {"requests": 47, "tokens": 29_000_000}}
    )

    result = await handler.get_usage(1, 2, "claude-x")
    assert [w.label for w in result.windows] == ["Kimi 5-hour"]
    assert result.windows[0].used_percent is None
    assert result.windows[0].used_count == 47


@pytest.mark.anyio
async def test_get_usage_skips_synthesis_without_service_or_meter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    handler._usage_meter = SimpleNamespace(
        rolling_usage=lambda: {"claude": {"requests": 47, "tokens": 1}}
    )
    # No third-party service in env -> no synthesis even with a meter.
    result = await handler.get_usage(1, 2, None)
    assert result.windows == ()

    # Service set but meter raises -> windows stay empty, no exception.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    def _broken():
        raise RuntimeError("meter gone")
    handler._usage_meter = SimpleNamespace(rolling_usage=_broken)
    result = await handler.get_usage(1, 2, None)
    assert result.service == "Kimi Code"
    assert result.windows == ()


@pytest.mark.anyio
async def test_get_usage_observed_windows_win_over_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    handler._usage_meter = SimpleNamespace(
        rolling_usage=lambda: {"claude": {"requests": 47, "tokens": 1}}
    )
    handler._claude_rate_limit = UsageSnapshot(
        provider="claude",
        windows=(UsageWindow(label="five hour", used_percent=50.0),),
    )

    result = await handler.get_usage(1, 2, "claude-x")
    assert [w.label for w in result.windows] == ["five hour"]
    assert result.windows[0].used_percent == 50.0


def test_synthesize_service_windows_reads_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", "600")
    window = synthesize_service_windows("Kimi Code", {"requests": 37, "tokens": 1})[0]
    assert window.count_limit == 600


def test_synthesize_service_windows_rejects_bad_limit_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", "600; rm -rf /")
    window = synthesize_service_windows("Kimi Code", {"requests": 37})[0]
    assert window.count_limit is None
    monkeypatch.setenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", str(10**9))
    window = synthesize_service_windows("Kimi Code", {"requests": 37})[0]
    assert window.count_limit is None


def test_render_usage_count_window_with_limit_shows_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CCC_USAGE_KIMI_5H_REQUEST_LIMIT",):
        monkeypatch.delenv(name, raising=False)
    snapshot = UsageSnapshot(
        provider="claude",
        service="Kimi Code",
        windows=(
            UsageWindow(
                label="Kimi 5-hour",
                used_percent=None,
                used_count=37,
                count_unit="req",
                count_limit=600,
            ),
        ),
    )
    rendered = render_usage(snapshot)
    assert (
        "- Kimi 5-hour: 37/600 req · 6.2% used / 93.8% left (local estimate)"
        in rendered
    )
    # Over-limit usage clamps the left percentage at zero.
    over = UsageSnapshot(
        provider="claude",
        windows=(
            UsageWindow(
                label="Kimi 5-hour",
                used_percent=None,
                used_count=650,
                count_unit="req",
                count_limit=600,
            ),
        ),
    )
    over_rendered = render_usage(over)
    assert "108.3% used / 0% left" in over_rendered


def test_synthesize_service_windows_weekly_requires_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weekly = {"requests": 125, "tokens": 53_498_062}
    # Without a configured weekly limit the weekly window stays hidden and the
    # 5-hour window still renders count-only.
    monkeypatch.delenv("CCC_USAGE_KIMI_WEEKLY_TOKEN_LIMIT", raising=False)
    monkeypatch.delenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", raising=False)
    windows = synthesize_service_windows(
        "Kimi Code", {"requests": 79, "tokens": 26_000_000}, weekly
    )
    assert [w.label for w in windows] == ["Kimi 5-hour"]
    # With both limits configured both windows appear in spec order.
    monkeypatch.setenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", "359")
    monkeypatch.setenv("CCC_USAGE_KIMI_WEEKLY_TOKEN_LIMIT", "222908592")
    windows = synthesize_service_windows(
        "Kimi Code", {"requests": 79, "tokens": 26_000_000}, weekly
    )
    assert [w.label for w in windows] == ["Kimi 5-hour", "Kimi weekly"]
    five_hour, weekly_window = windows
    assert (five_hour.used_count, five_hour.count_limit) == (79, 359)
    assert weekly_window.used_count == 53_498_062
    assert weekly_window.count_unit == "tok"
    assert weekly_window.count_limit == 222908592
    # Missing weekly aggregate hides only the weekly window.
    windows = synthesize_service_windows("Kimi Code", {"requests": 79}, None)
    assert [w.label for w in windows] == ["Kimi 5-hour"]


def test_render_usage_weekly_window_shows_percentages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", raising=False)
    monkeypatch.delenv("CCC_USAGE_KIMI_WEEKLY_TOKEN_LIMIT", raising=False)
    snapshot = UsageSnapshot(
        provider="claude",
        service="Kimi Code",
        windows=(
            UsageWindow(
                label="Kimi weekly",
                used_percent=None,
                used_count=53_498_062,
                count_unit="tok",
                count_limit=222_908_592,
            ),
        ),
    )
    rendered = render_usage(snapshot)
    assert (
        "- Kimi weekly: 53,498,062/222,908,592 tok · 24% used / 76% left "
        "(local estimate)" in rendered
    )


@pytest.mark.anyio
async def test_get_usage_synthesizes_weekly_window_from_meter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_service_env(monkeypatch)
    monkeypatch.setenv("CCC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    monkeypatch.setenv("ANTHROPIC_MODEL", "k3[1m]")
    monkeypatch.setenv("CCC_USAGE_KIMI_5H_REQUEST_LIMIT", "359")
    monkeypatch.setenv("CCC_USAGE_KIMI_WEEKLY_TOKEN_LIMIT", "222908592")
    handler = ProjectChatHandler.__new__(ProjectChatHandler)
    handler._agent_runtime = None
    handler._claude_usage = {}
    handler._clock = SimpleNamespace(time=lambda: 1000.0)
    handler._config = SimpleNamespace(claude_settings_path=tmp_path / "settings.json")
    handler._usage_meter = SimpleNamespace(
        rolling_usage=lambda: {"claude": {"requests": 79, "tokens": 26_000_000}},
        period_usage=lambda days=7: {
            "claude": {"requests": 125, "tokens": 53_498_062}
        },
    )

    result = await handler.get_usage(1, 2, "claude-x")
    assert [w.label for w in result.windows] == ["Kimi 5-hour", "Kimi weekly"]
    assert result.windows[1].count_limit == 222908592
