"""Contract for the provider-neutral lifecycle observation layer (#645)."""

from __future__ import annotations

import json

from telegram_bot.core.lifecycle_observation import (
    LifecycleEventType,
    LifecycleObservation,
    normalize_claude_hook,
    normalize_codex_app_server,
)
from telegram_bot.utils.redaction import contains_credential, redact_credentials

# Assembled at runtime so no scanner-flaggable literal lives in the source.
GH_TOKEN = "ghp_" + "a" * 30
BOT_TOKEN = "123456789:" + "A" * 24


def _codex_tool(**over):
    item = {"id": "i1", "type": "commandExecution", "status": "completed", "exitCode": 0}
    item.update(over.pop("item", {}))
    params = {"threadId": "t1", "turnId": "u1", "item": item}
    params.update(over)
    return {"method": "item/completed", "params": params}


# --------------------------------------------------------------------------- #
# 1. Both providers normalize to the same versioned schema.
# --------------------------------------------------------------------------- #

def test_claude_and_codex_tool_events_share_the_schema() -> None:
    claude = normalize_claude_hook(
        "PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"}
    )
    codex = normalize_codex_app_server(_codex_tool())
    assert claude.event is LifecycleEventType.TOOL_COMPLETED
    assert codex.event is LifecycleEventType.TOOL_COMPLETED
    assert claude.schema_version == codex.schema_version == 1
    assert claude.provider == "claude" and codex.provider == "codex"
    # Correlation ids are opaque hashes, never the raw session/thread id.
    assert claude.session_ref and "s1" not in claude.session_ref
    assert codex.session_ref and "t1" not in codex.session_ref
    assert codex.turn_ref and "u1" not in codex.turn_ref


def test_event_type_coverage() -> None:
    assert normalize_claude_hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s"}).event is LifecycleEventType.PROMPT_SUBMITTED
    assert normalize_claude_hook("Stop", {"session_id": "s"}).event is LifecycleEventType.TURN_COMPLETED
    assert normalize_claude_hook("SessionEnd", {"session_id": "s"}).event is LifecycleEventType.SESSION_CLOSED
    assert normalize_claude_hook("Notification", {"session_id": "s"}).event is LifecycleEventType.PROVIDER_NOTIFICATION
    assert normalize_codex_app_server({"method": "turn/started", "params": {"threadId": "t", "turnId": "u"}}).event is LifecycleEventType.PROMPT_SUBMITTED
    assert normalize_codex_app_server({"method": "turn/completed", "params": {"turn": {"status": "completed"}}}).event is LifecycleEventType.TURN_COMPLETED
    assert normalize_codex_app_server({"method": "item/commandExecution/requestApproval", "params": {"turnId": "u"}}).event is LifecycleEventType.PROVIDER_NOTIFICATION


# --------------------------------------------------------------------------- #
# 2. Body-free: credentials never land in a record.
# --------------------------------------------------------------------------- #

def test_credential_prompt_flags_without_storing_raw() -> None:
    obs = normalize_claude_hook("UserPromptSubmit", {"prompt": "here is " + GH_TOKEN, "session_id": "s"})
    assert obs.flag == "possible-raw-credential"
    blob = json.dumps(obs.to_record())
    # The raw prompt/token is never persisted (the event name legitimately
    # contains the word "prompt"; the raw value must not).
    assert GH_TOKEN not in blob
    assert "here is " not in blob


def test_clean_prompt_has_no_flag() -> None:
    obs = normalize_claude_hook("UserPromptSubmit", {"prompt": "just a normal question", "session_id": "s"})
    assert obs.flag is None


def test_tool_record_carries_only_a_shape_never_the_command() -> None:
    obs = normalize_claude_hook(
        "PostToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "curl h://x?token=" + GH_TOKEN}, "session_id": "s"},
    )
    blob = json.dumps(obs.to_record())
    assert obs.target_shape == "command"
    assert GH_TOKEN not in blob and "curl" not in blob


# --------------------------------------------------------------------------- #
# 3/4. Read-only + malformed + status.
# --------------------------------------------------------------------------- #

def test_read_only_tools_are_not_observed() -> None:
    assert normalize_claude_hook("PostToolUse", {"tool_name": "Read", "session_id": "s"}) is None
    assert normalize_codex_app_server(_codex_tool(item={"type": "fileRead"})) is None


def test_non_tool_codex_items_are_ignored() -> None:
    assert normalize_codex_app_server(_codex_tool(item={"type": "agentMessage"})) is None
    assert normalize_codex_app_server(_codex_tool(item={"type": "reasoning"})) is None


def test_malformed_events_return_none_not_raise() -> None:
    assert normalize_claude_hook("PostToolUse", "not-a-mapping") is None  # type: ignore[arg-type]
    assert normalize_claude_hook("UnknownEvent", {"session_id": "s"}) is None
    assert normalize_codex_app_server({"method": "item/completed", "params": {}}) is None
    assert normalize_codex_app_server({"method": "unknown/method", "params": {}}) is None
    assert normalize_codex_app_server("nope") is None  # type: ignore[arg-type]


def test_failed_tool_status() -> None:
    obs = normalize_codex_app_server(_codex_tool(item={"type": "commandExecution", "status": "failed", "exitCode": 1}))
    assert obs.tool_status == "failure"


def test_identical_events_share_a_dedup_key() -> None:
    a = normalize_codex_app_server(_codex_tool())
    b = normalize_codex_app_server(_codex_tool())
    assert a.dedup_key() == b.dedup_key()
    c = normalize_codex_app_server(_codex_tool(item={"id": "i2", "type": "commandExecution", "status": "completed", "exitCode": 0}))
    assert c.dedup_key() != a.dedup_key()


def test_provider_must_be_valid() -> None:
    import pytest

    with pytest.raises(ValueError):
        LifecycleObservation(event=LifecycleEventType.TURN_COMPLETED, provider="gpt")


# --------------------------------------------------------------------------- #
# Shared redaction module.
# --------------------------------------------------------------------------- #

def test_shared_redaction_covers_token_shapes() -> None:
    assert contains_credential(GH_TOKEN)
    assert contains_credential(BOT_TOKEN)
    assert not contains_credential("nothing secret here")
    redacted = redact_credentials("bot " + BOT_TOKEN + " gh " + GH_TOKEN)
    assert BOT_TOKEN not in redacted and GH_TOKEN not in redacted
    assert "[REDACTED_CREDENTIAL]" in redacted
