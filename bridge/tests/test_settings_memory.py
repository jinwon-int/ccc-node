"""Architecture contract for the memory settings domain (#584 P2-3)."""

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
}


def test_config_composes_one_plain_memory_settings_domain() -> None:
    assert set(MemorySettingsMixin.__annotations__) == set(MEMORY_FIELDS)
    assert MemorySettingsMixin in Config.__mro__
    assert not issubclass(MemorySettingsMixin, BaseSettings)
    assert "hook_policy_environment" in MemorySettingsMixin.__dict__

    for name, alias in MEMORY_FIELDS.items():
        assert Config.model_fields[name].alias == alias
