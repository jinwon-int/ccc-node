"""「한데」 inbound attachments (#2606): frame contract, spool take-over, HandeBot run.

Contract: family-messenger ``docs/FLEET-WORKER.md`` "첨부 turn — ``attachment``".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.hande.attachments import (
    AttachmentFrameError,
    adopt,
    parse_frame_attachment,
)
from telegram_bot.core.hande.bot import HandeBot
from telegram_bot.core.hande.state import HandeConfigError, validate_config
from telegram_bot.core.matrix.bot import ATTACHMENT_FAILED, ATTACHMENT_FAILED_DEFAULT
from telegram_bot.core.matrix.media import AttachmentError

PNG = b"\x89PNG\r\n\x1a\n fake image bytes"


def _spool(tmp_path: Path) -> Path:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700, exist_ok=True)
    return spool


def _put(spool: Path, data: bytes, *, mode: int = 0o600) -> Path:
    path = spool / f"{secrets.token_hex(16)}.bin"
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def _frame(path: Path, data: bytes, **overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "v": 1,
        "path": str(path),
        "kind": "image",
        "mime": "image/png",
        "name": "사진.png",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "caption": "",
    }
    frame.update(overrides)
    return frame


# -- frame contract ------------------------------------------------------------


def test_parse_accepts_the_contract_shape_and_ignores_unknown_keys(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    path = _put(spool, PNG)
    parsed = parse_frame_attachment({**_frame(path, PNG), "future": 1}, str(spool))
    assert parsed == {**_frame(path, PNG)}
    no_name = dict(_frame(path, PNG))
    del no_name["name"]
    assert parse_frame_attachment(no_name, str(spool))["name"] is None
    pdf = _frame(path, PNG, kind="file", mime="application/pdf")
    assert parse_frame_attachment(pdf, str(spool))["kind"] == "file"


def test_parse_ignores_an_unknown_version(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    assert parse_frame_attachment(_frame(_put(spool, PNG), PNG, v=2), str(spool)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"v": True},
        {"v": "1"},
        {"path": "relative/0123456789abcdef0123456789abcdef.bin"},
        {"path": "/elsewhere/0123456789abcdef0123456789abcdef.bin"},
        {"path": "SPOOL/../0123456789abcdef0123456789abcdef.bin"},
        {"path": "SPOOL/photo.jpg"},
        {"path": "SPOOL/sub/0123456789abcdef0123456789abcdef.bin"},
        {"kind": "file"},  # image MIME must be kind image
        {"kind": "image", "mime": "application/pdf"},
        {"kind": "video"},
        {"mime": "image"},
        {"mime": "image/png; charset=x"},
        {"name": ""},
        {"name": "a\u0007b"},
        {"name": 7},
        {"size": 0},
        {"size": True},
        {"size": "12"},
        {"sha256": "ABC"},
        {"sha256": "0" * 63},
        {"caption": None},
        {"caption": "가" * 4000},
    ],
)
def test_parse_rejects_contract_violations(tmp_path: Path, overrides: dict[str, Any]) -> None:
    spool = _spool(tmp_path)
    frame = _frame(_put(spool, PNG), PNG)
    for key, value in overrides.items():
        frame[key] = value.replace("SPOOL", str(spool)) if isinstance(value, str) else value
    with pytest.raises(AttachmentFrameError):
        parse_frame_attachment(frame, str(spool))


def test_parse_rejects_a_missing_required_key(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    for key in ("v", "path", "kind", "mime", "size", "sha256", "caption"):
        frame = _frame(_put(spool, PNG), PNG)
        del frame[key]
        with pytest.raises(AttachmentFrameError):
            parse_frame_attachment(frame, str(spool))
    with pytest.raises(AttachmentFrameError):
        parse_frame_attachment([], str(spool))


# -- spool take-over -----------------------------------------------------------


def _adopt(tmp_path: Path, frame: dict[str, Any], *, limit: int = 10_000_000) -> Path:
    spool = _spool(tmp_path)
    parsed = parse_frame_attachment(frame, str(spool))
    assert parsed is not None
    return adopt(parsed, spool_dir=str(spool), staging_dir=tmp_path / "staging", limit=limit)


def test_adopt_moves_the_file_into_private_staging(tmp_path: Path) -> None:
    source = _put(_spool(tmp_path), PNG)
    staged = _adopt(tmp_path, _frame(source, PNG))
    assert not source.exists()  # ownership moved: the bot has nothing left to delete
    assert staged.parent == tmp_path / "staging"
    assert staged.name.startswith("document_") and staged.suffix == ".png"
    assert staged.read_bytes() == PNG
    assert staged.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "staging").stat().st_mode & 0o777 == 0o700


def test_adopt_names_a_document_by_its_display_name(tmp_path: Path) -> None:
    data = b"%PDF-1.7 fake"
    source = _put(_spool(tmp_path), data)
    frame = _frame(source, data, kind="file", mime="application/pdf", name="report.pdf")
    assert _adopt(tmp_path, frame).suffix == ".pdf"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda frame, data: frame.update(sha256=hashlib.sha256(b"other").hexdigest()), "integrity"),
        (lambda frame, data: frame.update(size=len(data) - 1), "integrity"),
    ],
)
def test_adopt_deletes_a_file_that_fails_verification(tmp_path: Path, mutate: Any, reason: str) -> None:
    source = _put(_spool(tmp_path), PNG)
    frame = _frame(source, PNG)
    mutate(frame, PNG)
    with pytest.raises(AttachmentError) as error:
        _adopt(tmp_path, frame)
    assert error.value.reason == reason
    assert not source.exists()
    assert list((tmp_path / "staging").iterdir()) == []


def test_adopt_refuses_a_file_that_is_not_private(tmp_path: Path) -> None:
    source = _put(_spool(tmp_path), PNG, mode=0o644)
    with pytest.raises(AttachmentError) as error:
        _adopt(tmp_path, _frame(source, PNG))
    assert error.value.reason == "invalid"
    assert list((tmp_path / "staging").iterdir()) == []


def test_adopt_never_follows_a_planted_symlink(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(PNG)
    os.chmod(secret, 0o600)
    link = _spool(tmp_path) / f"{secrets.token_hex(16)}.bin"
    link.symlink_to(secret)
    with pytest.raises(AttachmentError) as error:
        _adopt(tmp_path, _frame(link, PNG))
    assert error.value.reason == "invalid"
    assert secret.read_bytes() == PNG  # the target is untouched
    assert list((tmp_path / "staging").iterdir()) == []


def test_adopt_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = _spool(tmp_path) / f"{secrets.token_hex(16)}.bin"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(AttachmentError) as error:
        _adopt(tmp_path, _frame(fifo, PNG))
    assert error.value.reason == "invalid"


def test_adopt_missing_file_and_oversize(tmp_path: Path) -> None:
    spool = _spool(tmp_path)
    gone = spool / f"{secrets.token_hex(16)}.bin"
    with pytest.raises(AttachmentError) as error:
        _adopt(tmp_path, _frame(gone, PNG))
    assert error.value.reason == "missing"
    source = _put(spool, PNG)
    with pytest.raises(AttachmentError) as error:
        _adopt(tmp_path, _frame(source, PNG), limit=len(PNG) - 1)
    assert error.value.reason == "oversize"
    assert source.exists()  # never taken over: still the bot's to delete


# -- config ----------------------------------------------------------------------


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "account": "@bridgenode:hande",
        "owner": "@owner:hande",
        "rooms": ["agent"],
        "family_rooms": [],
        "family_users": [],
        "socket_path": "/run/ccc-hande/responder.sock",
    }
    config.update(overrides)
    return config


def test_attachment_knobs_default_off_and_normalise_the_spool() -> None:
    config = validate_config(_config())
    assert config["attachment_frames"] is False and config["attachment_spool_dir"] is None
    config = validate_config(_config(attachment_frames=True, attachment_spool_dir="/var/spool//x/"))
    assert config["attachment_frames"] is True
    assert config["attachment_spool_dir"] == "/var/spool/x"


@pytest.mark.parametrize(
    "overrides",
    [
        {"attachment_frames": True},
        {"attachment_frames": "yes", "attachment_spool_dir": "/x"},
        {"attachment_spool_dir": "relative/spool"},
        {"attachment_spool_dir": "~/spool"},
        {"attachment_spool_dir": "/"},
        {"attachment_spool_dir": 3},
    ],
)
def test_attachment_knobs_fail_closed(overrides: dict[str, Any]) -> None:
    with pytest.raises(HandeConfigError):
        validate_config(_config(**overrides))


# -- HandeBot run ----------------------------------------------------------------


def _bot(tmp_path: Path) -> tuple[HandeBot, Path, list[str]]:
    spool = _spool(tmp_path)
    config_path = tmp_path / "hande.json"
    config_path.write_text(
        json.dumps(_config(attachment_frames=True, attachment_spool_dir=str(spool))),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    settings = SimpleNamespace(
        agent_provider="codex",
        project_root=tmp_path,
        execution_profile="owner-operator",
        bash_policy="approve-each",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        bot_data_dir=tmp_path / "data",
        hande_config_path=config_path,
        hande_socket_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        auto_new_session_after_hours=None,
        matrix_startup_banner=False,
        channel="hande",
        max_document_size_mb=10,
        image_context_guard=False,
    )
    bot = HandeBot(settings, project_chat=object(), session_manager=object())
    prompts: list[str] = []
    seen: dict[str, Any] = {}

    async def run_message(body: str, **kwargs: Any) -> Any:
        prompts.append(body)
        path = Path(body.split("Local ", 1)[1].split("path: ", 1)[1].split("\n", 1)[0])
        seen["bytes"] = path.read_bytes()
        return SimpleNamespace(text="answer", session_id=None, streamed=False, status="complete")

    bot._run_message = run_message  # type: ignore[method-assign]
    bot.seen = seen  # type: ignore[attr-defined]
    return bot, spool, prompts


async def _run(bot: HandeBot, attachment: Any) -> Any:
    job = {"event_id": "t-agent-1", "room_id": "agent", "body": "(fallback)", "attachment": attachment}
    return await bot._run_attachment(job, user_id=1, chat_id=1, room_id="agent", sink=object())


@pytest.mark.anyio
async def test_hande_image_runs_with_the_hande_image_prompt_and_is_deleted(tmp_path: Path) -> None:
    bot, spool, prompts = _bot(tmp_path)
    source = _put(spool, PNG)
    attachment = parse_frame_attachment(_frame(source, PNG), str(spool))
    result = await _run(bot, attachment)
    assert result.text == "answer"
    assert prompts[0].startswith("The user sent an inbound Hande image.")
    assert "Matrix" not in prompts[0] and ".png" in prompts[0]
    assert "Please describe what is in the image" in prompts[0]  # empty caption → default
    assert bot.seen["bytes"] == PNG  # type: ignore[attr-defined]
    assert not source.exists()
    assert list((tmp_path / "data" / "matrix-media").iterdir()) == []


@pytest.mark.anyio
async def test_hande_document_caption_is_the_instruction(tmp_path: Path) -> None:
    bot, spool, prompts = _bot(tmp_path)
    data = b"%PDF-1.7 fake"
    source = _put(spool, data)
    frame = _frame(source, data, kind="file", mime="application/pdf", name="report.pdf", caption="요약해 줘")
    await _run(bot, parse_frame_attachment(frame, str(spool)))
    assert prompts[0].startswith("The user sent an inbound Hande document.")
    assert '"report.pdf"' in prompts[0] and "MIME type: application/pdf" in prompts[0]
    assert "Caption / user instruction: 요약해 줘" in prompts[0]


@pytest.mark.anyio
async def test_hande_attachment_failures_answer_once_without_running_the_agent(tmp_path: Path) -> None:
    bot, spool, prompts = _bot(tmp_path)
    source = _put(spool, PNG)
    tampered = parse_frame_attachment(
        _frame(source, PNG, sha256=hashlib.sha256(b"x").hexdigest()), str(spool)
    )
    assert (await _run(bot, tampered)).text == ATTACHMENT_FAILED["integrity"]
    gone = parse_frame_attachment(_frame(spool / f"{'0' * 32}.bin", PNG), str(spool))
    assert (await _run(bot, gone)).text == ATTACHMENT_FAILED_DEFAULT
    assert (await _run(bot, "not-a-mapping")).text == ATTACHMENT_FAILED["invalid"]
    assert prompts == []
    assert list((tmp_path / "data" / "matrix-media").iterdir()) == []


@pytest.mark.anyio
async def test_hande_staged_file_is_deleted_when_the_turn_fails(tmp_path: Path) -> None:
    bot, spool, _prompts = _bot(tmp_path)

    async def boom(body: str, **kwargs: Any) -> Any:
        raise RuntimeError("provider failed")

    bot._run_message = boom  # type: ignore[method-assign]
    source = _put(spool, PNG)
    with pytest.raises(RuntimeError, match="provider failed"):
        await _run(bot, parse_frame_attachment(_frame(source, PNG), str(spool)))
    assert list((tmp_path / "data" / "matrix-media").iterdir()) == []


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
