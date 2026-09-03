"""Explicit Claude Agent SDK stdout NDJSON buffer bound.

Incident (2026-08-03 18:19:14 KST): the bridge lost a whole turn to

    claude_agent_sdk._internal.query - ERROR - Fatal error in message reader:
    Failed to decode JSON: JSON message exceeded maximum buffer size of
    1048576 bytes

The bridge never set ``ClaudeAgentOptions.max_buffer_size``, so the SDK used
its own 1 MiB ``_DEFAULT_MAX_BUFFER_SIZE`` and one oversized NDJSON line raised
``SDKJSONDecodeError`` inside the message reader task — which has no recovery
path. The line was 1,056,854 bytes: a 510 KB PNG read through the Read tool,
resized by the CLI to 682x2000 (528,000 base64 chars) and then emitted TWICE in
the same message (``message.content[].source.data`` and
``toolUseResult.file.base64``). The duplication is what doubles the payload, so
a single image above ~524 KB of base64 was enough to kill the bridge.

These tests pin the three things that keep that from recurring: the default is
generous, an operator can retune it through the environment, and the value
actually reaches ``ClaudeAgentOptions`` on every construction path.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from telegram_bot.core.claude_runtime import ClaudeRuntime
from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.runtime_config_check import (
    DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
    MAX_CLAUDE_MAX_BUFFER_SIZE,
    MIN_CLAUDE_MAX_BUFFER_SIZE,
)


BRIDGE_DIR = Path(__file__).resolve().parents[1]

# The exact NDJSON line that killed the reader, and the SDK limit it exceeded.
MEASURED_FATAL_LINE_BYTES = 1_056_854
SDK_FALLBACK_BUFFER_BYTES = 1024 * 1024


async def _reject(_tool_name, _tool_input, _context):
    raise AssertionError("can_use_tool must not run while building options")


def _runtime_settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    # Mirrors tests/test_claude_runtime_options.py: the adapter reads settings
    # by duck typing, so a namespace is enough to exercise _build_options.
    values = dict(
        execution_profile="strict-project",
        allowed_user_ids=[1],
        require_allowlist=True,
        bash_policy="auto-approve",
        claude_unrestricted=False,
        claude_cli_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        bridge_unsafe_shared_all_memory=False,
        bot_data_dir=tmp_path / ".telegram_bot",
        bridge_memory_audience_root=None,
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        hook_policy_environment=lambda: {"CCC_WIKI_MEMORY_ENABLED": "0"},
        bridge_web_mcp_mode="off",
        bridge_searxng_url=None,
        bridge_firecrawl_api_key=None,
        claude_max_buffer_size=DEFAULT_CLAUDE_MAX_BUFFER_SIZE,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _build(runtime: ClaudeRuntime, tmp_path: Path):
    return runtime._build_options(
        SessionRequest(working_directory=str(tmp_path)), _reject
    )


def _load_config_module(project_root: str):
    with patch.dict(
        os.environ,
        {"PROJECT_ROOT": project_root, "TELEGRAM_BOT_TOKEN": "123456:abc"},
        clear=True,
    ):
        sys.modules.pop("telegram_bot.utils.config", None)
        return importlib.import_module("telegram_bot.utils.config")


# --- (a) the default -------------------------------------------------------


def test_default_clears_the_measured_fatal_line_by_a_wide_margin() -> None:
    # 1 MiB was not "almost enough": the fatal line beat it by only 8,278
    # bytes, so any new default must leave real headroom rather than shave the
    # observed case.
    assert DEFAULT_CLAUDE_MAX_BUFFER_SIZE == 16 * 1024 * 1024
    assert DEFAULT_CLAUDE_MAX_BUFFER_SIZE > SDK_FALLBACK_BUFFER_BYTES
    assert DEFAULT_CLAUDE_MAX_BUFFER_SIZE > MEASURED_FATAL_LINE_BYTES * 8
    assert MIN_CLAUDE_MAX_BUFFER_SIZE == SDK_FALLBACK_BUFFER_BYTES


def test_sdk_fallback_constant_still_matches_the_incident() -> None:
    # Anchor the rationale to the installed SDK rather than a remembered
    # number. If the SDK ever raises its own default, this fails loudly and
    # the rationale above gets revisited instead of silently going stale.
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            _DEFAULT_MAX_BUFFER_SIZE,
        )
    except ImportError:  # pragma: no cover - private symbol moved
        pytest.skip("SDK private buffer constant not importable")
    assert _DEFAULT_MAX_BUFFER_SIZE == SDK_FALLBACK_BUFFER_BYTES
    assert MEASURED_FATAL_LINE_BYTES > _DEFAULT_MAX_BUFFER_SIZE


def test_config_default_is_the_shared_constant() -> None:
    with TemporaryDirectory() as td:
        module = _load_config_module(td)
        # Construct under a cleared environment: a live node may export
        # CCC_CLAUDE_MAX_BUFFER_SIZE, and the pydantic env source would
        # otherwise override the declared default.
        with patch.dict(
            os.environ,
            {"PROJECT_ROOT": td, "TELEGRAM_BOT_TOKEN": "123456:abc"},
            clear=True,
        ):
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
        assert cfg.claude_max_buffer_size == DEFAULT_CLAUDE_MAX_BUFFER_SIZE


def test_env_example_documents_the_actual_default() -> None:
    # tests/test_config_env_example.py proves the alias is mentioned; this
    # pins the documented number to the code so operators are not told 1 MiB.
    env_example = (BRIDGE_DIR / ".env.example").read_text(encoding="utf-8")
    assert (
        f"CCC_CLAUDE_MAX_BUFFER_SIZE={DEFAULT_CLAUDE_MAX_BUFFER_SIZE}" in env_example
    )


# --- (b) the environment override ------------------------------------------


def test_config_reads_explicit_env_override() -> None:
    with TemporaryDirectory() as td:
        module = _load_config_module(td)
        with patch.dict(
            os.environ,
            {"CCC_CLAUDE_MAX_BUFFER_SIZE": str(32 * 1024 * 1024)},
            clear=False,
        ):
            cfg = module.Config(telegram_bot_token="123456:abc", _env_file=None)
        assert cfg.claude_max_buffer_size == 32 * 1024 * 1024


@pytest.mark.parametrize(
    "value",
    [
        str(SDK_FALLBACK_BUFFER_BYTES - 1),  # below the SDK's own default
        str(MAX_CLAUDE_MAX_BUFFER_SIZE + 1),  # implausible, likely a typo
        "0",
    ],
)
def test_config_rejects_out_of_range_overrides(value: str) -> None:
    # A bound *lower* than the SDK fallback would be strictly worse than the
    # bug this replaces, so it must fail configuration rather than load.
    from pydantic import ValidationError

    with TemporaryDirectory() as td:
        module = _load_config_module(td)
        with patch.dict(
            os.environ, {"CCC_CLAUDE_MAX_BUFFER_SIZE": value}, clear=False
        ):
            with pytest.raises(ValidationError):
                module.Config(telegram_bot_token="123456:abc", _env_file=None)


# --- (c) it actually reaches ClaudeAgentOptions ------------------------------


def test_configured_value_reaches_claude_agent_options(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(
        settings=_runtime_settings(tmp_path, claude_max_buffer_size=24 * 1024 * 1024)
    )
    options = _build(runtime, tmp_path)
    assert options.max_buffer_size == 24 * 1024 * 1024


def test_bare_runtime_never_falls_back_to_the_sdk_default(tmp_path: Path) -> None:
    # The settings-free construction path (unit tests, conformance harness,
    # any future caller that forgets to bind settings) was the one remaining
    # route back to a 1 MiB reader.
    options = _build(ClaudeRuntime(), tmp_path)
    assert options.max_buffer_size == DEFAULT_CLAUDE_MAX_BUFFER_SIZE
    assert options.max_buffer_size is not None


def test_settings_bound_default_reaches_options(tmp_path: Path) -> None:
    options = _build(ClaudeRuntime(settings=_runtime_settings(tmp_path)), tmp_path)
    assert options.max_buffer_size == DEFAULT_CLAUDE_MAX_BUFFER_SIZE


@pytest.mark.parametrize(
    "bogus", [None, 0, -1, "16777216", True, 512, MAX_CLAUDE_MAX_BUFFER_SIZE + 1]
)
def test_unusable_settings_degrade_to_the_default(tmp_path: Path, bogus) -> None:
    # A malformed or absent value must not start a session with an unbounded
    # or 1 MiB reader, and must not raise during session construction either.
    runtime = ClaudeRuntime(
        settings=_runtime_settings(tmp_path, claude_max_buffer_size=bogus)
    )
    options = _build(runtime, tmp_path)
    assert options.max_buffer_size == DEFAULT_CLAUDE_MAX_BUFFER_SIZE


def test_missing_attribute_settings_degrade_to_the_default(tmp_path: Path) -> None:
    settings = _runtime_settings(tmp_path)
    del settings.claude_max_buffer_size
    options = _build(ClaudeRuntime(settings=settings), tmp_path)
    assert options.max_buffer_size == DEFAULT_CLAUDE_MAX_BUFFER_SIZE
