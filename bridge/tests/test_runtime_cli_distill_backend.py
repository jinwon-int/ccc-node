import json
from pathlib import Path
import sys

import pytest

from telegram_bot.memory.distill_extraction import build_extraction_input
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    DistillTrigger,
    TranscriptMessage,
)
from telegram_bot.memory.runtime_cli_backend import (
    RuntimeCliDistillBackend,
    RuntimeDistillBackendError,
    _command,
    _minimal_environment,
)


def _snapshot() -> CodexTranscriptSnapshot:
    messages = (TranscriptMessage("user", "remember this", None),)
    return CodexTranscriptSnapshot(
        thread_hash="a" * 64,
        last_turn_id="turn-1",
        messages=messages,
        byte_count=len(messages[0].text.encode()),
        truncated=False,
        captured_at="2026-08-05T00:00:00Z",
    )


def _stub(tmp_path: Path) -> Path:
    executable = tmp_path / "extractor-stub"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
value = json.load(sys.stdin)
json.dump({{
    "schema_version": 1,
    "provenance": {{
        "provider": value["provider"],
        "source_thread_hash": value["source_thread_hash"],
        "trigger": value["trigger"],
        "distilled_at": "2026-08-05T00:00:01Z"
    }},
    "honcho": [],
    "wiki_candidates": [],
    "resume": {{
        "last_activity": "",
        "pending_action": "",
        "awaiting_user": False,
        "open_question": "",
        "next_step": "",
        "evidence": []
    }}
}}, sys.stdout)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _reasonless_decision_stub(tmp_path: Path) -> Path:
    executable = tmp_path / "reasonless-extractor-stub"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
value = json.load(sys.stdin)
json.dump({{
    "schema_version": 1,
    "provenance": {{
        "provider": value["provider"],
        "source_thread_hash": value["source_thread_hash"],
        "trigger": value["trigger"],
        "distilled_at": "2026-08-05T00:00:01Z"
    }},
    "honcho": [{{
        "kind": "decision",
        "text": "Keep the source-only phase.",
        "subject": "session",
        "because": None
    }}],
    "wiki_candidates": [],
    "resume": {{
        "last_activity": "",
        "pending_action": "",
        "awaiting_user": False,
        "open_question": "",
        "next_step": "",
        "evidence": []
    }}
}}, sys.stdout)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _script(tmp_path: Path, name: str, body: str) -> Path:
    executable = tmp_path / name
    executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable


@pytest.mark.anyio
@pytest.mark.parametrize("extractor", ["claude", "piri"])
async def test_runtime_backends_return_the_same_validated_contract(
    tmp_path: Path,
    extractor: str,
) -> None:
    backend = RuntimeCliDistillBackend(
        extractor,  # type: ignore[arg-type]
        executable=str(_stub(tmp_path)),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )
    extraction_input = build_extraction_input(
        _snapshot(),
        trigger=DistillTrigger.EXPLICIT,
        provider="piri",
    )

    result = await backend.extract(extraction_input)

    assert result.provenance.provider == "piri"
    assert result.provenance.source_thread_hash == "a" * 64


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["claude", "piri"])
async def test_runtime_backends_reject_new_reasonless_decisions(
    tmp_path: Path,
    provider: str,
) -> None:
    backend = RuntimeCliDistillBackend(
        provider,  # type: ignore[arg-type]
        executable=str(_reasonless_decision_stub(tmp_path)),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )
    extraction_input = build_extraction_input(
        _snapshot(),
        trigger=DistillTrigger.EXPLICIT,
        provider="piri",
    )

    with pytest.raises(
        RuntimeDistillBackendError,
        match="^distill_decision_reason_missing$",
    ):
        await backend.extract(extraction_input)


def test_commands_disable_session_tools_and_context() -> None:
    claude = _command("claude", "/safe/claude", "provider-default", '{"type": "object"}')
    assert "--no-session-persistence" in claude
    assert claude[claude.index("--tools") + 1] == ""
    assert "mcp__*" in claude
    assert '{"type": "object"}' in claude[claude.index("--append-system-prompt") + 1]

    piri = _command("piri", "/safe/piri", "kimi-coding/k3", '{"type": "object"}')
    for flag in (
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
    ):
        assert flag in piri
    assert piri[-2:] == ("--model", "kimi-coding/k3")


def test_minimal_environment_drops_unrelated_secrets(tmp_path: Path) -> None:
    source = {
        "HOME": str(tmp_path),
        "KIMI_API_KEY": "synthetic-kimi-key",
        "TELEGRAM_BOT_TOKEN": "synthetic-telegram-token",
    }
    environment = _minimal_environment(source, provider="piri", temp_root=tmp_path)
    assert environment["KIMI_API_KEY"] == "synthetic-kimi-key"
    assert "TELEGRAM_BOT_TOKEN" not in environment


def _fenced_stub(tmp_path: Path) -> Path:
    return _script(
        tmp_path,
        "extractor-fenced-stub",
        """import json
import sys
value = json.load(sys.stdin)
body = json.dumps({
    "schema_version": 1,
    "provenance": {
        "provider": value["provider"],
        "source_thread_hash": value["source_thread_hash"],
        "trigger": value["trigger"],
        "distilled_at": "2026-08-05T00:00:01Z"
    },
    "honcho": [],
    "wiki_candidates": [],
    "resume": {
        "last_activity": "",
        "pending_action": "",
        "awaiting_user": False,
        "open_question": "",
        "next_step": "",
        "evidence": []
    }
})
sys.stdout.write("```json\\n" + body + "\\n```\\n")
""",
    )


@pytest.mark.anyio
async def test_runtime_backend_unwraps_a_single_markdown_fence(tmp_path: Path) -> None:
    backend = RuntimeCliDistillBackend(
        "piri",
        executable=str(_fenced_stub(tmp_path)),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )
    extraction_input = build_extraction_input(
        _snapshot(),
        trigger=DistillTrigger.EXPLICIT,
        provider="piri",
    )

    result = await backend.extract(extraction_input)

    assert result.provenance.provider == "piri"


def test_minimal_environment_keeps_ccc_piri_real_cli_path(tmp_path: Path) -> None:
    source = {
        "HOME": str(tmp_path),
        "CCC_PIRI_REAL_CLI_PATH": "/opt/piri/piri-ccc.sh",
    }
    environment = _minimal_environment(source, provider="piri", temp_root=tmp_path)
    assert environment["CCC_PIRI_REAL_CLI_PATH"] == "/opt/piri/piri-ccc.sh"


@pytest.mark.anyio
async def test_runtime_backend_failures_are_body_free(tmp_path: Path) -> None:
    extraction_input = build_extraction_input(
        _snapshot(), trigger=DistillTrigger.EXPLICIT, provider="claude"
    )
    failing = RuntimeCliDistillBackend(
        "piri",
        executable=str(_script(tmp_path, "exit-stub", "raise SystemExit(7)")),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )
    with pytest.raises(RuntimeDistillBackendError) as caught:
        await failing.extract(extraction_input)
    assert caught.value.code == "distill_nonzero_exit"
    assert caught.value.exit_status == 7
    assert str(caught.value) == "distill_nonzero_exit"

    invalid = RuntimeCliDistillBackend(
        "claude",
        executable=str(
            _script(tmp_path, "invalid-stub", 'print("PRIVATE_PROVIDER_BODY")')
        ),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )
    with pytest.raises(RuntimeDistillBackendError, match="^distill_output_invalid$"):
        await invalid.extract(extraction_input)


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["claude", "piri"])
async def test_runtime_backend_classifies_auth_failure_without_retaining_stderr(
    tmp_path: Path,
    provider: str,
) -> None:
    extraction_input = build_extraction_input(
        _snapshot(), trigger=DistillTrigger.EXPLICIT, provider="claude"
    )
    executable = _script(
        tmp_path,
        f"{provider}-auth-failure",
        'import sys\nsys.stderr.write("Authentication failed PRIVATE_TOKEN")\nraise SystemExit(1)',
    )
    backend = RuntimeCliDistillBackend(
        provider,  # type: ignore[arg-type]
        executable=str(executable),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )

    with pytest.raises(RuntimeDistillBackendError) as caught:
        await backend.extract(extraction_input)

    assert caught.value.code == "distill_auth_unavailable"
    assert "PRIVATE_TOKEN" not in repr(caught.value)


@pytest.mark.anyio
async def test_runtime_backend_also_classifies_provider_failure_written_to_stdout(
    tmp_path: Path,
) -> None:
    extraction_input = build_extraction_input(
        _snapshot(), trigger=DistillTrigger.EXPLICIT, provider="piri"
    )
    backend = RuntimeCliDistillBackend(
        "piri",
        executable=str(
            _script(
                tmp_path,
                "piri-quota-stdout",
                'print("usage limit reached PRIVATE_BODY")\nraise SystemExit(1)',
            )
        ),
        environment={"PATH": str(Path(sys.executable).parent)},
        temp_root=tmp_path,
    )

    with pytest.raises(RuntimeDistillBackendError) as caught:
        await backend.extract(extraction_input)

    assert caught.value.code == "distill_quota_exhausted"
    assert "PRIVATE_BODY" not in repr(caught.value)


def _backend(provider: str, model: str, env: dict[str, str]) -> RuntimeCliDistillBackend:
    return RuntimeCliDistillBackend(
        provider,  # type: ignore[arg-type]
        executable="claude",
        model=model,
        environment=env,
    )


def test_effective_model_reports_the_inherited_claude_selector() -> None:
    """`provider-default` is a sentinel, so the receipt must not repeat it.

    Measured on yukson: the bridge's distill lane inherited
    ANTHROPIC_MODEL=claude-opus-5 from its systemd unit while every journal
    entry read "provider-default", so which model ran could only be recovered
    from the unit file and live process argv.
    """

    backend = _backend("claude", "provider-default", {"ANTHROPIC_MODEL": "claude-opus-5"})

    assert backend.effective_model == "claude-opus-5"


def test_effective_model_prefers_an_explicit_model_over_the_environment() -> None:
    backend = _backend("claude", "haiku", {"ANTHROPIC_MODEL": "claude-opus-5"})

    assert backend.effective_model == "haiku"


def test_effective_model_keeps_the_sentinel_for_piri() -> None:
    """Piri's model is pinned by the launcher's own args, unseen by the bridge.

    Reporting the Claude variable there would be a guess, and a wrong receipt
    is worse than one that admits it does not know.
    """

    backend = _backend("piri", "provider-default", {"ANTHROPIC_MODEL": "claude-opus-5"})

    assert backend.effective_model == "provider-default"


@pytest.mark.parametrize("inherited", ["", "   ", "bad model name", "-leading-dash", "x" * 200])
def test_effective_model_falls_back_to_the_sentinel_for_unusable_values(inherited: str) -> None:
    backend = _backend("claude", "provider-default", {"ANTHROPIC_MODEL": inherited})

    assert backend.effective_model == "provider-default"


def test_effective_model_keeps_the_sentinel_when_nothing_is_inherited() -> None:
    backend = _backend("claude", "provider-default", {"PATH": "/usr/bin"})

    assert backend.effective_model == "provider-default"


def _argv_recording_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A Claude-lane stub that records its argv and inherited model env."""

    record = tmp_path / "argv.json"
    executable = tmp_path / "argv-recording-stub"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
value = json.load(sys.stdin)
with open({str(record)!r}, "w", encoding="utf-8") as handle:
    json.dump({{"argv": sys.argv[1:], "env_model": os.environ.get("ANTHROPIC_MODEL")}}, handle)
json.dump({{
    "schema_version": 1,
    "provenance": {{
        "provider": value["provider"],
        "source_thread_hash": value["source_thread_hash"],
        "trigger": value["trigger"],
        "distilled_at": "2026-08-05T00:00:01Z"
    }},
    "honcho": [],
    "wiki_candidates": [],
    "resume": {{
        "last_activity": "",
        "pending_action": "",
        "awaiting_user": False,
        "open_question": "",
        "next_step": "",
        "evidence": []
    }}
}}, sys.stdout)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, record


def _recorded_model(record: Path) -> str | None:
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    if "--model" not in argv:
        return None
    return argv[argv.index("--model") + 1]


async def _extract_with(tmp_path: Path, model: str, env: dict[str, str]) -> tuple[RuntimeCliDistillBackend, Path]:
    executable, record = _argv_recording_stub(tmp_path)
    backend = RuntimeCliDistillBackend(
        "claude",
        executable=str(executable),
        model=model,
        environment={"PATH": str(Path(sys.executable).parent), **env},
        temp_root=tmp_path,
    )
    await backend.extract(
        build_extraction_input(_snapshot(), trigger=DistillTrigger.EXPLICIT, provider="claude")
    )
    return backend, record


@pytest.mark.anyio
async def test_claude_extractor_runs_the_model_the_receipt_reports(tmp_path: Path) -> None:
    """#2723: the receipt said Opus while the child ran settings.json's Fable.

    `provider-default` passed no --model and the minimal environment drops
    ANTHROPIC_MODEL, so the child CLI fell back to its own settings model.
    The inherited selector must reach the child as --model, and the receipt
    must name exactly that value.
    """

    backend, record = await _extract_with(
        tmp_path, "provider-default", {"ANTHROPIC_MODEL": "claude-opus-5-5"}
    )

    assert _recorded_model(record) == "claude-opus-5-5"
    assert backend.effective_model == _recorded_model(record)
    # The model travels as an argument, not through the trimmed environment.
    assert json.loads(record.read_text(encoding="utf-8"))["env_model"] is None


@pytest.mark.anyio
async def test_claude_extractor_keeps_an_explicit_configured_model(tmp_path: Path) -> None:
    backend, record = await _extract_with(
        tmp_path, "claude-haiku-5-5", {"ANTHROPIC_MODEL": "claude-opus-5-5"}
    )

    assert _recorded_model(record) == "claude-haiku-5-5"
    assert backend.effective_model == "claude-haiku-5-5"


@pytest.mark.anyio
async def test_claude_extractor_passes_no_model_without_a_usable_selector(tmp_path: Path) -> None:
    backend, record = await _extract_with(
        tmp_path, "provider-default", {"ANTHROPIC_MODEL": "bad model name"}
    )

    assert _recorded_model(record) is None
    assert backend.effective_model == "provider-default"
