"""Regression tests: crush turns pin an explicit workspace model (#926).

canary4 (2026-08-04): with the crush provider gates open, a model-less turn
reached crush with ``model=None``; crush activated its bundled provider
default (anthropic/claude-sonnet-4-6), inherited the bridge's
``ANTHROPIC_API_KEY``, and died on ``401 invalid x-api-key`` against
api.anthropic.com — surfaced to the user as "Processing failed:
Unauthorized". Three guards now prevent that silent fallback:

1. ``Settings.crush_model`` (CCC_CRUSH_MODEL) names the default crush
   workspace model in provider/model form.
2. ``_process_agent_message`` applies it to crush turns that did not choose
   a model explicitly, so the SessionRequest always carries the pin.
3. ``CrushServerClient`` strips inherited Anthropic credential env vars from
   the crush subprocess so the bundled provider cannot activate on leaked
   credentials; an explicit ``process_environment`` still passes through.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    SessionRequest,
    TextDeltaEvent,
)
from telegram_bot.core.crush_runtime import CrushServerClient
from telegram_bot.core.project_chat import ProjectChatHandler


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def config_module():
    # Fresh REAL config module: sibling tests may have left a (contained)
    # fake in sys.modules.
    sys.modules.pop("telegram_bot.utils.config", None)
    return importlib.import_module("telegram_bot.utils.config")


# -- 1. Settings alias --------------------------------------------------------


def test_crush_model_loads_from_env_alias(config_module, tmp_path: Path) -> None:
    settings = config_module.Settings.load(
        project_root=tmp_path / "project",
        environ={"TELEGRAM_BOT_TOKEN": "123456:test", "CCC_CRUSH_MODEL": "kimi/k3"},
        bot_env_file=tmp_path / "missing-package.env",
    )
    assert settings.crush_model == "kimi/k3"


def test_crush_model_defaults_to_none(config_module, tmp_path: Path) -> None:
    settings = config_module.Settings.load(
        project_root=tmp_path / "project",
        environ={"TELEGRAM_BOT_TOKEN": "123456:test"},
        bot_env_file=tmp_path / "missing-package.env",
    )
    assert settings.crush_model is None


# -- 2. Turn-path fallback ------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = dict(
        agent_provider="crush",
        crush_model="kimi/k3",
        project_root=tmp_path,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        session_guard_enabled=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def send_turn(self, message, *, approval_handler=None):
        async def stream():
            yield TextDeltaEvent("ok")
            yield CompletionEvent("end_turn")

        return stream()

    async def interrupt(self) -> None:
        return None


class _FakeRuntime:
    supports_session_browsing = False

    def __init__(self) -> None:
        self.requests: list[SessionRequest] = []

    async def start_or_resume(self, request: SessionRequest) -> _FakeSession:
        self.requests.append(request)
        return _FakeSession(request.session_id or f"new-{len(self.requests)}")

    async def close(self) -> None:
        return None

    async def recycle(self) -> bool:
        return True


def _handler(tmp_path: Path, runtime: _FakeRuntime, **overrides) -> ProjectChatHandler:
    handler = ProjectChatHandler(
        settings=_settings(tmp_path, **overrides), agent_runtime=runtime
    )
    handler._task_ledger_cache = False
    return handler


@pytest.mark.anyio
async def test_crush_turn_without_model_choice_uses_configured_default(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    handler = _handler(tmp_path, runtime)

    response = await handler.process_message("hi", user_id=7, chat_id=70)

    assert response.success is True
    assert runtime.requests[0].model == "kimi/k3"


@pytest.mark.anyio
async def test_explicit_model_choice_wins_over_configured_default(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    handler = _handler(tmp_path, runtime)

    response = await handler.process_message(
        "hi", user_id=7, chat_id=70, model="zai/glm-5.2"
    )

    assert response.success is True
    assert runtime.requests[0].model == "zai/glm-5.2"


@pytest.mark.anyio
async def test_crush_turn_without_configured_default_stays_unpinned(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    handler = _handler(tmp_path, runtime, crush_model=None)

    response = await handler.process_message("hi", user_id=7, chat_id=70)

    assert response.success is True
    assert runtime.requests[0].model is None


@pytest.mark.anyio
async def test_non_crush_provider_does_not_inherit_crush_default(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    handler = _handler(tmp_path, runtime, agent_provider="codex")

    response = await handler.process_message("hi", user_id=7, chat_id=70)

    assert response.success is True
    assert runtime.requests[0].model is None


# -- 3. Subprocess env scrub ----------------------------------------------------


def test_inherited_env_strips_anthropic_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))

    client = CrushServerClient()

    assert "ANTHROPIC_API_KEY" not in client._env
    assert "ANTHROPIC_AUTH_TOKEN" not in client._env
    assert client._env.get("PATH")


def test_explicit_process_environment_passes_through_untouched() -> None:
    env = {"ANTHROPIC_API_KEY": "sk-explicit-choice", "FOO": "1"}
    client = CrushServerClient(process_environment=env)
    assert client._env == env


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
