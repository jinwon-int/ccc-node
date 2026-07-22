"""Architecture contract for the memory settings domain (#584 P2-3)."""

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from telegram_bot.utils.config import Config
from telegram_bot.utils.settings_memory import MemorySettingsMixin


MEMORY_FIELDS = {
    "node_isolation_profile": "CCC_NODE_ISOLATION_PROFILE",
    "wiki_memory_enabled": "CCC_WIKI_MEMORY_ENABLED",
    "memory_user_label": "CCC_MEMORY_USER_LABEL",
    "memory_assistant_label": "CCC_MEMORY_ASSISTANT_LABEL",
    "codex_memory_materializer_path": "CCC_CODEX_MEMORY_MATERIALIZER_PATH",
    "codex_memory_bootstrap_timeout_seconds": "CCC_CODEX_MEMORY_BOOTSTRAP_TIMEOUT_SEC",
    "codex_audience_auth_mode": "CCC_CODEX_AUDIENCE_AUTH_MODE",
    "bridge_memory_mode": "CCC_BRIDGE_MEMORY_MODE",
    "bridge_unsafe_shared_all_memory": "CCC_BRIDGE_UNSAFE_SHARED_ALL_MEMORY",
    "bridge_memory_audience_root": "CCC_BRIDGE_MEMORY_AUDIENCE_ROOT",
    "bridge_memory_audience_key_path": "CCC_BRIDGE_MEMORY_AUDIENCE_KEY_PATH",
    "codex_distill_checkpoint_turns": "CCC_CODEX_DISTILL_CHECKPOINT_TURNS",
    "codex_distill_checkpoint_bytes": "CCC_CODEX_DISTILL_CHECKPOINT_BYTES",
    "codex_distill_checkpoint_age_seconds": "CCC_CODEX_DISTILL_CHECKPOINT_AGE_SECONDS",
    "codex_distill_model": "CCC_CODEX_DISTILL_MODEL",
    "codex_distill_timeout_seconds": "CCC_CODEX_DISTILL_TIMEOUT_SEC",
}


def test_config_composes_one_plain_memory_settings_domain() -> None:
    assert set(MemorySettingsMixin.__annotations__) == set(MEMORY_FIELDS)
    assert MemorySettingsMixin in Config.__mro__
    assert not issubclass(MemorySettingsMixin, BaseSettings)
    assert "hook_policy_environment" in MemorySettingsMixin.__dict__

    for name, alias in MEMORY_FIELDS.items():
        assert Config.model_fields[name].alias == alias


def test_codex_checkpoint_gates_are_disabled_by_default() -> None:
    assert Config.model_fields["codex_distill_checkpoint_turns"].default == 0
    assert Config.model_fields["codex_distill_checkpoint_bytes"].default == 0
    assert Config.model_fields["codex_distill_checkpoint_age_seconds"].default == 0

    configured = Config(
        telegram_bot_token="123456:abc",
        _env_file=None,
        CCC_CODEX_DISTILL_CHECKPOINT_TURNS=12,
        CCC_CODEX_DISTILL_CHECKPOINT_BYTES=65_536,
        CCC_CODEX_DISTILL_CHECKPOINT_AGE_SECONDS=21_600,
    )
    assert configured.codex_distill_checkpoint_turns == 12
    assert configured.codex_distill_checkpoint_bytes == 65_536
    assert configured.codex_distill_checkpoint_age_seconds == 21_600


def test_codex_distill_provider_cost_settings_are_explicit_and_bounded() -> None:
    assert Config.model_fields["codex_distill_model"].default == "provider-default"
    assert Config.model_fields["codex_distill_timeout_seconds"].default == 120.0

    configured = Config(
        telegram_bot_token="123456:abc",
        _env_file=None,
        CCC_CODEX_DISTILL_MODEL="gpt-5-mini",
        CCC_CODEX_DISTILL_TIMEOUT_SEC=45.5,
    )
    assert configured.codex_distill_model == "gpt-5-mini"
    assert configured.codex_distill_timeout_seconds == 45.5

    for model in ("", "private model", "x" * 129):
        with pytest.raises(ValidationError, match="CCC_CODEX_DISTILL_MODEL"):
            Config(
                telegram_bot_token="123456:abc",
                _env_file=None,
                CCC_CODEX_DISTILL_MODEL=model,
            )
    for timeout in (0, 601):
        with pytest.raises(ValidationError, match="CCC_CODEX_DISTILL_TIMEOUT_SEC"):
            Config(
                telegram_bot_token="123456:abc",
                _env_file=None,
                CCC_CODEX_DISTILL_TIMEOUT_SEC=timeout,
            )


@pytest.mark.parametrize(
    ("alias", "value"),
    [
        ("CCC_CODEX_DISTILL_CHECKPOINT_TURNS", -1),
        ("CCC_CODEX_DISTILL_CHECKPOINT_TURNS", 1_001),
        ("CCC_CODEX_DISTILL_CHECKPOINT_BYTES", 16 * 1024 * 1024 + 1),
        ("CCC_CODEX_DISTILL_CHECKPOINT_AGE_SECONDS", 7 * 24 * 60 * 60 + 1),
    ],
)
def test_codex_checkpoint_gates_have_hard_bounds(alias: str, value: int) -> None:
    with pytest.raises(ValidationError, match="CCC_CODEX_DISTILL_CHECKPOINT"):
        Config(
            telegram_bot_token="123456:abc",
            _env_file=None,
            **{alias: value},
        )
