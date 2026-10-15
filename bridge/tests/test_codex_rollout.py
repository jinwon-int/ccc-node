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


# --- Codex CLI 0.149+/0.160 item format (#2316) -------------------------------

FIXTURE_0160 = Path(__file__).parent / "fixtures" / "codex_rollout_0_160_1.jsonl"
FIXTURE_0160_ID = "00000000-0000-7000-8000-000000002316"
CAPTURED_0160 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SPEECH_0160_NEWEST_FIRST = [
    ("assistant", "synthetic answer two"),
    ("assistant", "synthetic progress note"),
    ("user", "synthetic question two"),
    ("assistant", "synthetic answer one"),
    ("user", "synthetic question one"),
]


def _install_0160(root: Path, extra: list[str] | None = None) -> Path:
    sessions = root / "sessions" / "2026" / "10" / "11"
    sessions.mkdir(mode=0o755, parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-10-11T11-00-00-{FIXTURE_0160_ID}.jsonl"
    body = FIXTURE_0160.read_text(encoding="utf-8")
    path.write_text(body + "".join(line + "\n" for line in extra or ()), encoding="utf-8")
    path.chmod(0o644)
    return path


def _non_speech_0160() -> list[str]:
    """The fixture's own tool/reasoning/token records, re-stamped after the speech."""

    lines = []
    for raw in FIXTURE_0160.read_text(encoding="utf-8").splitlines():
        record = json.loads(raw)
        payload = record["payload"]
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        if record["type"] == "response_item" and payload.get("type") != "message":
            lines.append(record)
        elif payload.get("type") == "token_count" or item.get("type") in {"CommandExecution", "Reasoning"}:
            lines.append(record)
    assert len(lines) >= 4
    restamped = []
    for record in lines:
        payload = dict(record["payload"])
        if "turn_id" in payload:
            payload["turn_id"] = "turn-0002"
        restamped.append(
            json.dumps({**record, "timestamp": "2026-10-11T11:20:00.000Z", "payload": payload})
        )
    return restamped


def test_codex_0160_item_completed_speech_is_read(tmp_path: Path) -> None:
    """0.160.1 writes no user_message event; speech lives in item_completed."""

    path = _install_0160(tmp_path)
    assert b'"user_message"' not in path.read_bytes()

    messages, last_turn_id, truncated = read_codex_rollout_candidates(
        path, FIXTURE_0160_ID, limits=TranscriptBounds(), captured=CAPTURED_0160
    )

    assert [(m.role, m.text) for m in messages] == SPEECH_0160_NEWEST_FIRST
    assert last_turn_id == "turn-0002"
    assert truncated is False
    joined = " ".join(m.text for m in messages)
    for leaked in ("SYNTHETIC-DEVELOPER", "SYNTHETIC-INJECTED", "SYNTHETIC-TOOL", "SYNTHETIC-REASONING", "SYNTHETIC-DIFF"):
        assert leaked not in joined


def test_mixed_legacy_and_item_speech_is_not_double_counted(tmp_path: Path) -> None:
    """One utterance in both spellings counts once; legacy-only speech survives."""

    item = {"type": "item_completed", "turn_id": "turn-1"}
    path = _write_rollout(
        tmp_path,
        "sess-mixed",
        [
            _line("event_msg", {"type": "user_message", "message": "같은 질문"}, NOW - timedelta(minutes=9)),
            _line("event_msg", {**item, "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "같은 질문"}]}}, NOW - timedelta(minutes=9)),
            _line("event_msg", {**item, "item": {"type": "AgentMessage", "id": "a1", "content": [{"type": "Text", "text": "첫 줄"}, {"type": "Text", "text": "둘째 줄"}]}}, NOW - timedelta(minutes=8)),
            _line("event_msg", {"type": "agent_message", "message": "첫 줄 둘째 줄"}, NOW - timedelta(minutes=8)),
            _line("event_msg", {"type": "agent_message", "message": "레거시에만 있는 답"}, NOW - timedelta(minutes=7)),
            _line("response_item", {"type": "agent_message", "author": "x", "recipient": "y", "content": [{"type": "input_text", "text": "inter-agent traffic"}]}, NOW - timedelta(minutes=7)),
        ],
    )

    messages, _turn, _truncated = read_codex_rollout_candidates(
        path, "sess-mixed", limits=TranscriptBounds(), captured=NOW
    )

    assert [(m.role, m.text) for m in messages] == [
        ("assistant", "레거시에만 있는 답"),
        ("assistant", "첫 줄\n둘째 줄"),
        ("user", "같은 질문"),
    ]


def test_tool_heavy_tail_does_not_exhaust_the_item_budget(tmp_path: Path) -> None:
    """Non-speech records after the last message must not hide the speech."""

    tail = _non_speech_0160() * 80  # >= 320 non-speech records after the speech
    path = _install_0160(tmp_path, tail)

    messages, last_turn_id, _truncated = read_codex_rollout_candidates(
        path, FIXTURE_0160_ID, limits=TranscriptBounds(), captured=CAPTURED_0160
    )

    assert [(m.role, m.text) for m in messages] == SPEECH_0160_NEWEST_FIRST
    assert last_turn_id == "turn-0002"


def test_speech_beyond_the_first_tail_window_is_found_within_the_byte_bound(
    tmp_path: Path,
) -> None:
    """A tail of megabytes of tool output widens once, bounded by bytes."""

    bulky = json.dumps(
        {
            "timestamp": "2026-10-11T11:30:00.000Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c", "output": "x" * (512 * 1024)},
        }
    )
    path = _install_0160(tmp_path, [bulky] * 6)  # ~3 MiB, past the 1.6 MiB default window

    messages, _turn, _truncated = read_codex_rollout_candidates(
        path, FIXTURE_0160_ID, limits=TranscriptBounds(), captured=CAPTURED_0160
    )

    assert [(m.role, m.text) for m in messages] == SPEECH_0160_NEWEST_FIRST


def test_item_budget_counts_speech_only(tmp_path: Path) -> None:
    path = _install_0160(tmp_path)

    messages, _turn, truncated = read_codex_rollout_candidates(
        path, FIXTURE_0160_ID, limits=TranscriptBounds(max_items=2), captured=CAPTURED_0160
    )

    assert [(m.role, m.text) for m in messages] == SPEECH_0160_NEWEST_FIRST[:2]
    assert truncated is True


def _bulky_tool_output(when: str, size: int = 512 * 1024) -> str:
    return json.dumps(
        {
            "timestamp": when,
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c", "output": "x" * size},
        }
    )


def _speech_item(role: str, text: str, minutes_ago: int) -> str:
    kind, part = ("UserMessage", "text") if role == "user" else ("AgentMessage", "Text")
    return _line(
        "event_msg",
        {
            "type": "item_completed",
            "turn_id": "turn-1",
            "item": {"type": kind, "id": f"{role}-{minutes_ago}", "content": [{"type": part, "text": text}]},
        },
        NOW - timedelta(minutes=minutes_ago),
    )


def test_agent_only_tail_widens_to_recover_the_user_turn(tmp_path: Path) -> None:
    """#2316 review MED: progress messages in the tail must not hide the user turn."""

    path = _write_rollout(
        tmp_path,
        "sess-agent-tail",
        [
            _speech_item("user", "the request that started the work", 30),
            *[_bulky_tool_output("2026-08-19T11:40:00Z")] * 4,  # ~2 MiB, past the default window
            _speech_item("assistant", "progress update", 5),
            _speech_item("assistant", "final answer", 1),
        ],
    )

    messages, _turn, truncated = read_codex_rollout_candidates(
        path, "sess-agent-tail", limits=TranscriptBounds(), captured=NOW
    )

    assert [(m.role, m.text) for m in messages] == [
        ("assistant", "final answer"),
        ("assistant", "progress update"),
        ("user", "the request that started the work"),
    ]
    assert truncated is False  # the widened read reached the start of the file


def test_a_tail_read_that_misses_the_file_start_is_flagged_truncated(tmp_path: Path) -> None:
    path = _write_rollout(
        tmp_path,
        "sess-partial",
        [
            _speech_item("user", "older question", 60),
            *[_bulky_tool_output("2026-08-19T11:10:00Z")] * 4,
            _speech_item("user", "newer question", 2),
            _speech_item("assistant", "newer answer", 1),
        ],
    )

    messages, _turn, truncated = read_codex_rollout_candidates(
        path, "sess-partial", limits=TranscriptBounds(), captured=NOW
    )

    assert [(m.role, m.text) for m in messages] == [
        ("assistant", "newer answer"),
        ("user", "newer question"),
    ]
    assert truncated is True


def test_repeated_short_message_in_another_turn_is_not_deduplicated(tmp_path: Path) -> None:
    """#2316 review LOW: dedupe pairs neighbouring records only, not the whole file."""

    path = _write_rollout(
        tmp_path,
        "sess-repeat",
        [
            _line("event_msg", {"type": "user_message", "message": "ok"}, NOW - timedelta(minutes=50)),
            *[
                _line("event_msg", {"type": "token_count", "info": None}, NOW - timedelta(minutes=40))
                for _ in range(20)
            ],
            _speech_item("assistant", "between", 20),
            _speech_item("user", "ok", 3),
        ],
    )

    messages, _turn, _truncated = read_codex_rollout_candidates(
        path, "sess-repeat", limits=TranscriptBounds(), captured=NOW
    )

    assert [(m.role, m.text) for m in messages] == [
        ("user", "ok"),
        ("assistant", "between"),
        ("user", "ok"),
    ]
