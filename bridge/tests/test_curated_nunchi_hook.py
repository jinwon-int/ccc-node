"""#2844: the curated settings layer carries nunchi's SessionStart hook only
for the owner's own bridge.

``setting_sources=[]`` sessions (unrestricted owner-operator, audience-scoped,
every non-owner profile) never read ``settings.local.json``, where the
installer registers ``nunchi/sessionstart.sh``. The curated layer is shared by
non-owner profiles, so the hook — which injects the owner's node-global nunchi
snapshot — is added only under the same privacy gate the node-global write
sink uses (``owner_global_memory_gate``), never for an audience route or the
``external`` isolation profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.curated_memory import (
    build_curated_memory_settings,
    owner_nunchi_injection_allowed,
)
from telegram_bot.utils.memory_policy import owner_global_memory_gate

OWNER_ID = 934719283


def _settings(tmp_path: Path, **overrides):
    values = dict(
        bridge_memory_mode="curated",
        execution_profile="owner-operator",
        allowed_user_ids=[OWNER_ID],
        require_allowlist=True,
        telegram_session_scope="per-user-chat",
        bridge_unsafe_shared_all_memory=False,
        node_isolation_profile="fleet",
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        hook_policy_environment=lambda: {"CCC_NODE_ISOLATION_PROFILE": "fleet"},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _session_start_commands(raw: str | None) -> list[str]:
    assert raw is not None
    payload = json.loads(raw)
    return [
        hook["command"]
        for matcher in payload["hooks"]["SessionStart"]
        for hook in matcher["hooks"]
    ]


def _has_nunchi(raw: str | None) -> bool:
    return any("nunchi/sessionstart.sh" in command for command in _session_start_commands(raw))


def test_owner_operator_curated_session_gets_the_nunchi_hook(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    commands = _session_start_commands(build_curated_memory_settings(settings))
    assert any("load-memory.sh SessionStart" in command for command in commands)
    nunchi = [command for command in commands if "nunchi/sessionstart.sh" in command]
    assert nunchi == [f"bash {tmp_path / '.claude' / 'hooks' / 'nunchi' / 'sessionstart.sh'}"]


def test_shared_groups_scope_is_still_the_owners_bridge(tmp_path: Path) -> None:
    assert _has_nunchi(build_curated_memory_settings(_settings(tmp_path, telegram_session_scope="shared-groups")))


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution_profile": "strict-project"},
        {"execution_profile": "disabled"},
        # owner-operator with two users degrades to disabled (not the owner's own)
        {"allowed_user_ids": [OWNER_ID, 812345678]},
        {"allowed_user_ids": []},
        {"require_allowlist": False},
    ],
    ids=["strict-project", "disabled", "two-users", "no-users", "no-allowlist"],
)
def test_non_owner_profiles_never_get_the_nunchi_hook(tmp_path: Path, overrides) -> None:
    settings = _settings(tmp_path, **overrides)
    raw = build_curated_memory_settings(settings)
    assert not _has_nunchi(raw)
    # the rest of the curated lifecycle is unchanged
    assert any("load-memory.sh SessionStart" in c for c in _session_start_commands(raw))


def test_shared_all_scope_never_gets_the_nunchi_hook(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        telegram_session_scope="shared_all",  # alias spelling must not bypass
        bridge_unsafe_shared_all_memory=True,  # legacy curated override
    )
    assert not _has_nunchi(build_curated_memory_settings(settings))


def test_external_isolation_profile_never_gets_the_nunchi_hook(tmp_path: Path) -> None:
    settings = _settings(tmp_path, node_isolation_profile="external")
    assert not _has_nunchi(build_curated_memory_settings(settings))


def test_audience_route_never_gets_the_global_nunchi_hook(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    audience = SimpleNamespace(hook_environment=lambda _settings: {"CCC_MEMORY_AUDIENCE": "private"})
    assert owner_nunchi_injection_allowed(settings, audience) is False
    assert owner_nunchi_injection_allowed(settings, None) is True


def test_off_and_audience_scoped_modes_add_nothing(tmp_path: Path) -> None:
    assert build_curated_memory_settings(_settings(tmp_path, bridge_memory_mode="off")) is None
    assert owner_nunchi_injection_allowed(_settings(tmp_path, bridge_memory_mode="audience-scoped"), None) is False


def test_gate_is_shared_with_the_global_write_sink(tmp_path: Path) -> None:
    allowed, profile, scope = owner_global_memory_gate(_settings(tmp_path))
    assert (allowed, profile, scope) == (True, "owner-operator", "per-user-chat")
    allowed, profile, _ = owner_global_memory_gate(_settings(tmp_path, allowed_user_ids=[1, 2]))
    assert (allowed, profile) == (False, "disabled")
    allowed, _, scope = owner_global_memory_gate(_settings(tmp_path, telegram_session_scope="shared-all"))
    assert (allowed, scope) == (False, "shared-all")
    allowed, _, _ = owner_global_memory_gate(_settings(tmp_path, bridge_memory_mode="audience-scoped"))
    assert allowed is False


def test_global_sink_uses_the_shared_gate() -> None:
    import inspect

    import telegram_bot.__main__ as entry

    source = inspect.getsource(entry._build_global_local_sink_worker)
    assert "owner_global_memory_gate(settings)" in source
