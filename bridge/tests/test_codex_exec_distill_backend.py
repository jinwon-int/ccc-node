"""Hermetic security contract for the isolated Codex distill backend (#478)."""

from __future__ import annotations

import asyncio
from functools import wraps
import json
import os
from pathlib import Path
import signal
from typing import Any, Awaitable, Callable, ParamSpec

import pytest

from telegram_bot.memory.codex_exec_backend import (
    DISTILL_EXTRACTION_PROMPT,
    CodexDistillBackendError,
    CodexExecDistillBackend,
)
from telegram_bot.memory.distill_extraction import (
    DISTILL_EXTRACTION_SCHEMA_VERSION,
    DistillExtractionInput,
    canonical_extraction_input_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "codex-distill-extraction-v1.schema.json"
THREAD_HASH = "a" * 64
P = ParamSpec("P")


def async_test(function: Callable[P, Awaitable[None]]) -> Callable[P, None]:
    """Run an async test without requiring an external pytest plugin."""

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


def extraction_input() -> DistillExtractionInput:
    return DistillExtractionInput.model_validate(
        {
            "schema_version": DISTILL_EXTRACTION_SCHEMA_VERSION,
            "provider": "codex",
            "content_trust": "untrusted",
            "source_thread_hash": THREAD_HASH,
            "trigger": "new_command",
            "captured_at": "2026-07-14T09:00:00Z",
            "truncated": False,
            "messages": [{"role": "user", "text": "harmless durable fact"}],
            "message_count": 1,
            "byte_count": len("harmless durable fact"),
        }
    )


def valid_output() -> dict[str, Any]:
    return {
        "schema_version": DISTILL_EXTRACTION_SCHEMA_VERSION,
        "provenance": {
            "provider": "codex",
            "source_thread_hash": THREAD_HASH,
            "trigger": "new_command",
            "distilled_at": "2026-07-14T09:01:00Z",
        },
        "honcho": [
            {"kind": "observation", "text": "A harmless fact was retained.", "subject": "session"}
        ],
        "wiki_candidates": [],
        "resume": {
            "last_activity": "Extracted a harmless fact.",
            "pending_action": "",
            "awaiting_user": False,
            "open_question": "",
            "next_step": "",
            "evidence": ["issue #478"],
        },
    }


class FakeProcess:
    def __init__(
        self,
        action: Callable[[bytes], Awaitable[None]],
        *,
        returncode: int = 0,
    ) -> None:
        self.pid = 47800
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._action = action
        self.terminate_calls = 0
        self.kill_calls = 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        await self._action(input or b"")
        self.returncode = self._final_returncode
        return b"ignored stdout", b"ignored stderr"

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -signal.SIGKILL


def output_path_from_args(args: tuple[str, ...]) -> Path:
    return Path(args[args.index("--output-last-message") + 1])


@async_test
async def test_backend_uses_exact_isolated_argv_private_cwd_and_canonical_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture: dict[str, Any] = {}

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        capture.update(args=args, kwargs=kwargs)
        cwd = Path(kwargs["cwd"])
        assert cwd.is_dir()
        assert list(cwd.iterdir()) == []

        async def action(stdin: bytes) -> None:
            capture["stdin"] = stdin
            output_path_from_args(args).write_text(json.dumps(valid_output()), encoding="utf-8")

        return FakeProcess(action)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex",
        schema_path=SCHEMA_PATH,
        temp_root=tmp_path,
        environment={
            "HOME": "/home/operator",
            "CODEX_HOME": "/home/operator/.codex",
            "CODEX_ACCESS_TOKEN": "synthetic-access-value",
            "TELEGRAM_BOT_TOKEN": "must-not-pass",
            "A2A_EDGE_SECRET": "must-not-pass",
            "HONCHO_API_KEY": "must-not-pass",
            "WIKI_AGENT_TOKEN": "must-not-pass",
            "GITHUB_TOKEN": "must-not-pass",
            "OPENAI_API_KEY": "must-not-pass",
        },
    )

    result = await backend.extract(extraction_input())

    args = capture["args"]
    assert args[0:2] == (str(Path("/usr/bin/codex").resolve()), "exec")
    assert args[-1] == DISTILL_EXTRACTION_PROMPT
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in args
    assert Path(args[args.index("--output-schema") + 1]) == SCHEMA_PATH.resolve()
    assert output_path_from_args(args).parent != Path(capture["kwargs"]["cwd"])
    assert capture["stdin"] == canonical_extraction_input_bytes(extraction_input())
    assert b"harmless durable fact" not in " ".join(args).encode()
    assert capture["kwargs"]["stdin"] is asyncio.subprocess.PIPE
    assert capture["kwargs"]["stdout"] is asyncio.subprocess.DEVNULL
    assert capture["kwargs"]["stderr"] is asyncio.subprocess.DEVNULL
    assert capture["kwargs"]["start_new_session"] is True
    assert callable(capture["kwargs"]["preexec_fn"])
    assert result.provenance.source_thread_hash == THREAD_HASH

    child_env = capture["kwargs"]["env"]
    assert child_env["CODEX_ACCESS_TOKEN"] == "synthetic-access-value"
    assert child_env["CODEX_HOME"] == "/home/operator/.codex"
    assert set(child_env) == {
        "HOME",
        "CODEX_HOME",
        "CODEX_ACCESS_TOKEN",
        "PATH",
        "TMPDIR",
        "TERM",
        "NO_COLOR",
        "RUST_LOG",
    }
    assert "synthetic-access-value" not in " ".join(args)
    assert b"synthetic-access-value" not in capture["stdin"]
    assert set(child_env).isdisjoint(
        {
            "TELEGRAM_BOT_TOKEN",
            "A2A_EDGE_SECRET",
            "HONCHO_API_KEY",
            "WIKI_AGENT_TOKEN",
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
        }
    )


@async_test
async def test_backend_rejects_output_provenance_not_bound_to_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = valid_output()
    payload["provenance"]["source_thread_hash"] = "b" * 64

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        async def action(_stdin: bytes) -> None:
            output_path_from_args(args).write_text(json.dumps(payload), encoding="utf-8")

        return FakeProcess(action)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex", schema_path=SCHEMA_PATH, temp_root=tmp_path
    )

    with pytest.raises(CodexDistillBackendError, match="^codex_distill_output_invalid$"):
        await backend.extract(extraction_input())


@async_test
@pytest.mark.parametrize(
    ("returncode", "spawn_error", "expected"),
    [
        (7, False, "codex_distill_nonzero_exit"),
        (0, True, "codex_distill_spawn_failed"),
    ],
)
async def test_backend_exposes_body_free_spawn_and_exit_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    spawn_error: bool,
    expected: str,
) -> None:
    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs
        if spawn_error:
            raise OSError("provider stderr with private body")

        async def action(_stdin: bytes) -> None:
            return None

        return FakeProcess(action, returncode=returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex", schema_path=SCHEMA_PATH, temp_root=tmp_path
    )

    with pytest.raises(CodexDistillBackendError) as caught:
        await backend.extract(extraction_input())

    assert str(caught.value) == expected
    assert "private body" not in repr(caught.value)


@async_test
async def test_timeout_terminates_the_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    never = asyncio.Event()
    killpg_calls: list[tuple[int, signal.Signals]] = []

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs

        async def action(_stdin: bytes) -> None:
            await never.wait()

        return FakeProcess(action)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killpg_calls.append((pid, sig)))
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex",
        schema_path=SCHEMA_PATH,
        temp_root=tmp_path,
        timeout_seconds=0.01,
    )

    with pytest.raises(CodexDistillBackendError, match="^codex_distill_timeout$"):
        await backend.extract(extraction_input())

    assert killpg_calls[0] == (47800, signal.SIGTERM)


@async_test
async def test_cancellation_terminates_process_group_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    never = asyncio.Event()
    killpg_calls: list[tuple[int, signal.Signals]] = []

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs

        async def action(_stdin: bytes) -> None:
            await never.wait()

        return FakeProcess(action)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killpg_calls.append((pid, sig)))
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex", schema_path=SCHEMA_PATH, temp_root=tmp_path
    )

    task = asyncio.create_task(backend.extract(extraction_input()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killpg_calls[0] == (47800, signal.SIGTERM)


@async_test
async def test_communicate_failure_terminates_process_group_and_hides_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    killpg_calls: list[tuple[int, signal.Signals]] = []
    process: FakeProcess

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        del args, kwargs

        async def action(_stdin: bytes) -> None:
            raise OSError("private transport detail")

        nonlocal process
        process = FakeProcess(action)
        return process

    def fake_killpg(pid: int, sig: signal.Signals) -> None:
        killpg_calls.append((pid, sig))
        process.returncode = -sig

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex", schema_path=SCHEMA_PATH, temp_root=tmp_path
    )

    with pytest.raises(CodexDistillBackendError) as caught:
        await backend.extract(extraction_input())

    assert str(caught.value) == "codex_distill_io_failed"
    assert "private transport detail" not in repr(caught.value)
    assert killpg_calls == [(47800, signal.SIGTERM)]


@async_test
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", "codex_distill_output_missing"),
        ("unsafe_mode", "codex_distill_output_unsafe"),
        ("symlink", "codex_distill_output_unsafe"),
        ("hardlink", "codex_distill_output_unsafe"),
        ("oversized", "codex_distill_output_too_large"),
        ("invalid", "codex_distill_output_invalid"),
    ],
)
async def test_backend_rejects_missing_unsafe_oversized_and_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        del kwargs
        output = output_path_from_args(args)

        async def action(_stdin: bytes) -> None:
            if mutation == "missing":
                output.unlink()
            elif mutation == "unsafe_mode":
                output.write_text(json.dumps(valid_output()), encoding="utf-8")
                output.chmod(0o644)
            elif mutation == "symlink":
                output.unlink()
                output.symlink_to(SCHEMA_PATH)
            elif mutation == "hardlink":
                sibling = output.with_name("sibling.json")
                output.write_text(json.dumps(valid_output()), encoding="utf-8")
                os.link(output, sibling)
            elif mutation == "oversized":
                output.write_bytes(b"x" * (64 * 1024 + 1))
            else:
                output.write_text("PRIVATE_PROVIDER_BODY not json", encoding="utf-8")

        return FakeProcess(action)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex", schema_path=SCHEMA_PATH, temp_root=tmp_path
    )

    with pytest.raises(CodexDistillBackendError) as caught:
        await backend.extract(extraction_input())

    assert str(caught.value) == expected
    assert "PRIVATE_PROVIDER_BODY" not in repr(caught.value)


@async_test
async def test_backend_rejects_unsafe_schema_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    schema_link = tmp_path / "schema.json"
    schema_link.symlink_to(SCHEMA_PATH)
    spawn_calls = 0

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        nonlocal spawn_calls
        del args, kwargs
        spawn_calls += 1
        raise AssertionError("must not spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    backend = CodexExecDistillBackend(
        executable="/usr/bin/codex", schema_path=schema_link, temp_root=tmp_path
    )

    with pytest.raises(CodexDistillBackendError, match="^codex_distill_schema_unsafe$"):
        await backend.extract(extraction_input())

    assert spawn_calls == 0
