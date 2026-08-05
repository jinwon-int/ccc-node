from types import SimpleNamespace

import pytest

from telegram_bot.memory.codex_exec_backend import CodexExecDistillBackend
from telegram_bot.memory.distill_backend_factory import (
    build_distill_backend,
    resolve_distill_model_timeout,
    resolve_distill_provider,
)
from telegram_bot.memory.runtime_cli_backend import RuntimeCliDistillBackend


@pytest.mark.parametrize("provider", ["claude", "codex", "piri"])
def test_auto_follows_supported_main_runtime(provider: str) -> None:
    assert resolve_distill_provider(provider, "auto") == provider


def test_auto_is_off_for_runtime_without_contract_adapter() -> None:
    assert resolve_distill_provider("crush", "auto") is None
    assert resolve_distill_provider("codex", "off") is None


def settings(**updates):
    values = {
        "memory_distill_model": "provider-default",
        "memory_distill_timeout_seconds": 120.0,
        "codex_distill_model": "provider-default",
        "codex_distill_timeout_seconds": 120.0,
        "codex_cli_path": "codex",
        "claude_cli_path": None,
        "piri_cli_path": "piri",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_factory_selects_backend_without_changing_contract() -> None:
    assert isinstance(
        build_distill_backend(settings(), provider="codex", wiki_enabled=True),
        CodexExecDistillBackend,
    )
    assert isinstance(
        build_distill_backend(settings(), provider="claude", wiki_enabled=True),
        RuntimeCliDistillBackend,
    )
    piri = build_distill_backend(settings(), provider="piri", wiki_enabled=True)
    assert isinstance(piri, RuntimeCliDistillBackend)
    assert piri.provider == "piri"


def test_legacy_codex_cost_override_applies_only_to_codex() -> None:
    configured = settings(
        codex_distill_model="gpt-5-mini",
        codex_distill_timeout_seconds=45.0,
    )
    assert resolve_distill_model_timeout(configured, "codex") == (
        "gpt-5-mini",
        45.0,
    )
    assert resolve_distill_model_timeout(configured, "piri") == (
        "provider-default",
        120.0,
    )
