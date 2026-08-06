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


def test_commands_disable_session_tools_and_context() -> None:
    claude = _command("claude", "/safe/claude", "provider-default")
    assert "--no-session-persistence" in claude
    assert claude[claude.index("--tools") + 1] == ""
    assert "mcp__*" in claude

    piri = _command("piri", "/safe/piri", "kimi-coding/k3")
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
