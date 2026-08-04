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

import asyncio
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    ErrorEvent,
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


# #938: the crush server reads providers and the read-only permission set only
# from CRUSH_GLOBAL_CONFIG. Measured on dungae (2026-08-04), a server started
# without it died with "No providers configured" — the bridge lane never
# carried the fleet crushrc that the headless runner stages.
def test_config_is_staged_into_a_directory_for_the_server(tmp_path: Path) -> None:
    source = tmp_path / "crushrc.readonly"
    source.write_text("provider add zai --type openai-compat\n", encoding="utf-8")

    client = CrushServerClient(config_path=source)

    staged = client._env.get("CRUSH_GLOBAL_CONFIG")
    assert staged, "server must be told where the fleet config lives"
    # crush treats the value as a directory and reads <dir>/crushrc from it.
    assert Path(staged).is_dir()
    assert (Path(staged) / "crushrc").read_text(encoding="utf-8") == source.read_text(
        encoding="utf-8"
    )


def test_staged_config_is_removed_on_close(tmp_path: Path) -> None:
    source = tmp_path / "crushrc.readonly"
    source.write_text("provider add zai --type openai-compat\n", encoding="utf-8")

    client = CrushServerClient(config_path=source)
    staged = Path(client._env["CRUSH_GLOBAL_CONFIG"])
    assert staged.is_dir()

    asyncio.run(client.close())

    # The config expands key files at load time, so it must not outlive the run.
    assert not staged.exists()


def test_explicit_process_environment_opts_out_of_staging(tmp_path: Path) -> None:
    source = tmp_path / "crushrc.readonly"
    source.write_text("provider add zai --type openai-compat\n", encoding="utf-8")
    chosen = tmp_path / "operator-dir"
    chosen.mkdir()

    client = CrushServerClient(
        process_environment={"CRUSH_GLOBAL_CONFIG": str(chosen)},
        config_path=source,
    )

    # A caller that hands over the whole environment keeps ownership of it.
    assert client._env == {"CRUSH_GLOBAL_CONFIG": str(chosen)}
    assert client._config_dir is None


def test_inherited_global_config_is_not_overridden(monkeypatch, tmp_path: Path) -> None:
    chosen = tmp_path / "node-dir"
    chosen.mkdir()
    monkeypatch.setenv("CRUSH_GLOBAL_CONFIG", str(chosen))
    source = tmp_path / "crushrc.readonly"
    source.write_text("provider add zai --type openai-compat\n", encoding="utf-8")

    client = CrushServerClient(config_path=source)

    assert client._env["CRUSH_GLOBAL_CONFIG"] == str(chosen)
    assert client._config_dir is None


# -- 4. Terminal failures must leave a server-side trace -----------------------


class _FailingSession(_FakeSession):
    """A provider turn that ends in a normalized terminal error."""

    def send_turn(self, message, *, approval_handler=None):
        async def stream():
            yield ErrorEvent(code="tool_use", message="tool_use", retryable=False)

        return stream()


class _FailingRuntime(_FakeRuntime):
    async def start_or_resume(self, request: SessionRequest) -> _FailingSession:
        self.requests.append(request)
        return _FailingSession(request.session_id or f"new-{len(self.requests)}")


@pytest.mark.anyio
async def test_terminal_error_is_logged_not_only_shown_to_the_user(
    tmp_path: Path, caplog
) -> None:
    # dungae (2026-08-04): a crush turn died with `tool_use`; the user saw
    # "Processing failed: tool_use" but bot.log and error_*.log were both
    # silent, so the failure left no server-side trace to diagnose from.
    runtime = _FailingRuntime()
    handler = _handler(tmp_path, runtime)

    with caplog.at_level("ERROR"):
        response = await handler.process_message("hi", user_id=7, chat_id=70)

    assert response.success is False
    assert "tool_use" in (response.error or "")

    logged = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert "Turn failed" in logged, "terminal failure must be logged"
    # The user-facing string carries only `message`; the log must add the
    # normalized fields an operator needs to triage.
    assert "code=tool_use" in logged
    assert "retryable=False" in logged
    assert "provider=crush" in logged


# -- 5. Finish reasons: both provider spellings mean the same thing -------------


@pytest.mark.parametrize(
    "reason",
    ["end_turn", "stop", "stop_sequence", "length", "max_tokens", "tool_calls", "tool_use"],
)
def test_normal_finish_reasons_complete_the_turn(reason: str) -> None:
    # dungae (2026-08-04): a GLM-5.2 turn that called a tool finished with
    # `tool_use` and surfaced as "Processing failed: tool_use". crush passes
    # each provider family's own spelling through, so the OpenAI and Anthropic
    # names for one concept must be treated alike:
    #   tool_calls / tool_use      length / max_tokens
    from telegram_bot.core.agent_runtime import CompletionEvent as _Completion
    from telegram_bot.core.crush_runtime import CrushRuntime, _ActiveTurn

    runtime = CrushRuntime(client_factory=lambda: None)
    active = _ActiveTurn(queue=asyncio.Queue(), approval_handler=None)
    runtime._complete_turn(active, {"reason": reason})

    drained = []
    while not active.queue.empty():
        drained.append(active.queue.get_nowait())

    assert any(isinstance(e, _Completion) for e in drained), (
        f"{reason!r} must end the turn normally, not as an error"
    )
    assert not any(isinstance(e, ErrorEvent) for e in drained), (
        f"{reason!r} was reported as a failure"
    )


def test_unknown_finish_reason_still_fails_the_turn() -> None:
    from telegram_bot.core.crush_runtime import CrushRuntime, _ActiveTurn

    runtime = CrushRuntime(client_factory=lambda: None)
    active = _ActiveTurn(queue=asyncio.Queue(), approval_handler=None)
    runtime._complete_turn(active, {"reason": "content_filter"})

    drained = []
    while not active.queue.empty():
        drained.append(active.queue.get_nowait())

    errors = [e for e in drained if isinstance(e, ErrorEvent)]
    assert errors and errors[0].message == "content_filter"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
