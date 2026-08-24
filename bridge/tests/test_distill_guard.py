from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from telegram_bot.memory.distill_guard import (
    COOLDOWN_ACTIVE_CODE,
    COOLDOWN_UNSAFE_CODE,
    GLOBAL_DISABLED_CODE,
    DistillGuard,
    classify_provider_failure,
    communicate_with_bounded_stderr,
    global_distill_disabled,
)


@pytest.mark.parametrize(
    ("provider", "diagnostic", "expected"),
    [
        ("claude", b"Authentication failed: login required", "distill_auth_unavailable"),
        ("codex", b"HTTP 429: too many requests", "distill_rate_limited"),
        ("piri", b"usage limit reached for this account", "distill_quota_exhausted"),
        ("piri", b"unknown model kimi-private", "distill_model_unavailable"),
    ],
)
def test_provider_failure_classification_is_shared_and_body_free(
    provider: str,
    diagnostic: bytes,
    expected: str,
) -> None:
    assert classify_provider_failure(provider, diagnostic) == expected


@pytest.mark.anyio
async def test_stderr_capture_drains_large_output_but_retains_only_the_bound() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdin.buffer.read(); sys.stderr.buffer.write(b'x' * 200000)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    diagnostic = await communicate_with_bounded_stderr(
        process,
        input_bytes=b"bounded input",
        max_stderr_bytes=4096,
    )

    assert process.returncode == 0
    assert diagnostic == b"x" * 4096


def test_global_marker_is_shared_across_legacy_and_bridge_paths(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    environment = {"CCC_STATE_DIR": str(state), "HOME": str(tmp_path)}
    assert global_distill_disabled(environment) is False

    marker = state / "distill.disabled"
    marker.touch()

    assert global_distill_disabled(environment) is True
    decision = DistillGuard(state_dir=state).decision("codex", "provider-default")
    assert decision.allowed is False
    assert decision.code == GLOBAL_DISABLED_CODE


def test_provider_model_cooldown_is_private_scoped_and_expires(tmp_path: Path) -> None:
    now = [1000.0]
    guard = DistillGuard(state_dir=tmp_path / "state", clock=lambda: now[0])

    tripped = guard.trip(
        "piri",
        "kimi-coding/k3",
        error_code="distill_rate_limited",
        cooldown_seconds=600,
    )

    assert tripped.code == COOLDOWN_ACTIVE_CODE
    path = guard.cooldown_path("piri", "kimi-coding/k3")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "version",
        "provider",
        "model",
        "error_code",
        "retry_after_epoch",
    }
    assert guard.decision("piri", "other-model").allowed is True
    assert guard.decision("codex", "kimi-coding/k3").allowed is True
    assert guard.decision("piri", "kimi-coding/k3").allowed is False

    now[0] = 1600.0
    assert guard.decision("piri", "kimi-coding/k3").allowed is True


def test_unsafe_cooldown_state_fails_closed_without_exposing_body(tmp_path: Path) -> None:
    guard = DistillGuard(state_dir=tmp_path / "state")
    path = guard.cooldown_path("claude", "provider-default")
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("PRIVATE PROVIDER BODY", encoding="utf-8")
    os.chmod(path, 0o644)

    decision = guard.decision("claude", "provider-default")

    assert decision.allowed is False
    assert decision.code == COOLDOWN_UNSAFE_CODE
    assert "PRIVATE" not in repr(decision)
