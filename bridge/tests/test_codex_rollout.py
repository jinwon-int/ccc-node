"""Disk fallback contract for Codex distill snapshots.

The app-server only knows a thread while it is live, but distill snapshots are
taken by a worker that runs afterwards, so every Codex extraction on a live node
read an empty transcript and stored nothing. These tests pin the rollout-file
fallback that replaces that silent loss.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from telegram_bot.memory.codex_rollout import (
    read_codex_rollout_candidates,
    validate_rollout_root,
)
from telegram_bot.memory.distill_types import TranscriptBounds

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _line(kind: str, payload: dict, when: datetime) -> str:
    return json.dumps(
        {
            "type": kind,
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "payload": payload,
        },
        ensure_ascii=False,
    )


def _write_rollout(
    root: Path,
    session_id: str,
    records: list[str],
    *,
    header_id: str | None = None,
    mode: int = 0o644,
) -> Path:
    sessions = root / "sessions" / "2026" / "08" / "19"
    sessions.mkdir(mode=0o755, parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-08-19T00-00-00-{session_id}.jsonl"
    header = _line(
        "session_meta",
        {"id": header_id or session_id, "session_id": header_id or session_id},
        NOW - timedelta(hours=2),
    )
    path.write_text("\n".join([header, *records]) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_extracts_user_and_agent_speech_newest_first(tmp_path: Path) -> None:
    path = _write_rollout(
        tmp_path,
        "sess-1",
        [
            _line("event_msg", {"type": "user_message", "message": "첫 질문"}, NOW - timedelta(minutes=9)),
            _line("event_msg", {"type": "agent_message", "message": "첫 답변"}, NOW - timedelta(minutes=8)),
            _line("turn_context", {"turn_id": "turn-9"}, NOW - timedelta(minutes=7)),
            _line("event_msg", {"type": "user_message", "message": "둘째 질문"}, NOW - timedelta(minutes=6)),
        ],
    )

    messages, last_turn_id, truncated = read_codex_rollout_candidates(
        path, "sess-1", limits=TranscriptBounds(), captured=NOW
    )

    assert [(m.role, m.text) for m in messages] == [
        ("user", "둘째 질문"),
        ("assistant", "첫 답변"),
        ("user", "첫 질문"),
    ]
    assert last_turn_id == "turn-9"
    assert truncated is False


def test_ignores_tool_reasoning_and_injected_response_items(tmp_path: Path) -> None:
    """Only real speech counts, matching what the RPC path admits.

    A ``response_item`` with role=user also carries injected context blocks that
    the user never typed, so counting them would put machine-generated preamble
    into memory as if the user had said it.
    """

    path = _write_rollout(
        tmp_path,
        "sess-2",
        [
            _line("event_msg", {"type": "user_message", "message": "진짜 발화"}, NOW - timedelta(minutes=5)),
            _line(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<recommended_plugins>주입된 컨텍스트"}],
                },
                NOW - timedelta(minutes=5),
            ),
            _line("response_item", {"type": "reasoning", "summary": []}, NOW - timedelta(minutes=4)),
            _line("response_item", {"type": "custom_tool_call", "name": "exec", "input": "rm -rf /"}, NOW - timedelta(minutes=4)),
            _line("response_item", {"type": "custom_tool_call_output", "output": "secret output"}, NOW - timedelta(minutes=3)),
            _line("world_state", {"full": True, "state": {"agents_md": {"text": "memory block"}}}, NOW - timedelta(minutes=3)),
        ],
    )

    messages, _last_turn_id, _truncated = read_codex_rollout_candidates(
        path, "sess-2", limits=TranscriptBounds(), captured=NOW
    )

    assert [(m.role, m.text) for m in messages] == [("user", "진짜 발화")]
    joined = " ".join(m.text for m in messages)
    for leaked in ("recommended_plugins", "rm -rf /", "secret output", "memory block"):
        assert leaked not in joined


def test_rejects_a_rollout_whose_header_names_another_session(tmp_path: Path) -> None:
    """The filename is not identity; a copied or renamed rollout must not pass."""

    path = _write_rollout(
        tmp_path,
        "sess-3",
        [_line("event_msg", {"type": "user_message", "message": "hi"}, NOW)],
        header_id="a-different-session",
    )

    with pytest.raises(ValueError, match="identity"):
        read_codex_rollout_candidates(
            path, "sess-3", limits=TranscriptBounds(), captured=NOW
        )


def test_rejects_group_writable_rollout_and_symlink(tmp_path: Path) -> None:
    path = _write_rollout(
        tmp_path,
        "sess-4",
        [_line("event_msg", {"type": "user_message", "message": "hi"}, NOW)],
        mode=0o664,
    )
    with pytest.raises(ValueError, match="unsafe"):
        read_codex_rollout_candidates(
            path, "sess-4", limits=TranscriptBounds(), captured=NOW
        )

    path.chmod(0o644)
    target = path.rename(path.with_suffix(".real"))
    os.symlink(target, path)
    with pytest.raises((ValueError, OSError)):
        read_codex_rollout_candidates(
            path, "sess-4", limits=TranscriptBounds(), captured=NOW
        )


def test_owner_only_modes_remain_readable(tmp_path: Path) -> None:
    """Codex writes 0644; a stricter mode must not be rejected as unsafe."""

    path = _write_rollout(
        tmp_path,
        "sess-5",
        [_line("event_msg", {"type": "user_message", "message": "hi"}, NOW)],
        mode=0o600,
    )

    messages, _turn, _truncated = read_codex_rollout_candidates(
        path, "sess-5", limits=TranscriptBounds(), captured=NOW
    )
    assert [m.text for m in messages] == ["hi"]


def test_messages_past_the_age_horizon_are_dropped_and_flagged(tmp_path: Path) -> None:
    path = _write_rollout(
        tmp_path,
        "sess-6",
        [
            _line("event_msg", {"type": "user_message", "message": "너무 오래된 발화"}, NOW - timedelta(days=30)),
            _line("event_msg", {"type": "user_message", "message": "최근 발화"}, NOW - timedelta(minutes=1)),
        ],
    )

    messages, _turn, truncated = read_codex_rollout_candidates(
        path, "sess-6", limits=TranscriptBounds(), captured=NOW
    )

    assert [m.text for m in messages] == ["최근 발화"]
    assert truncated is True


def test_message_count_bound_is_enforced(tmp_path: Path) -> None:
    path = _write_rollout(
        tmp_path,
        "sess-7",
        [
            _line("event_msg", {"type": "user_message", "message": f"m{index}"}, NOW - timedelta(minutes=20 - index))
            for index in range(20)
        ],
    )

    messages, _turn, truncated = read_codex_rollout_candidates(
        path, "sess-7", limits=TranscriptBounds(max_messages=3), captured=NOW
    )

    assert [m.text for m in messages] == ["m19", "m18", "m17"]
    assert truncated is True


def test_validate_rollout_root_rejects_a_world_writable_sessions_root(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # mkdir(mode=...) is masked by the process umask, so set it explicitly.
    sessions.chmod(0o777)
    with pytest.raises(ValueError, match="unsafe"):
        validate_rollout_root(sessions)

    sessions.chmod(0o755)
    validate_rollout_root(sessions)  # Codex's own 0755 must stay acceptable.
