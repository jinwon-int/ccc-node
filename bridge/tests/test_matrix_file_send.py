"""Agent deliverables reach a Matrix direct room as encrypted files (#2001)."""

from __future__ import annotations

import json
from pathlib import Path
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core import deliverables
from telegram_bot.core.matrix import media as inbound_media
from telegram_bot.core.matrix import outbound_media
from telegram_bot.core.matrix.bot import FILES_DIRECT_ONLY, FILES_SKIPPED, MAX_FILES_PER_TURN
from telegram_bot.core.matrix.state import FILE_JOB_BODY
from telegram_bot.core.matrix.transport import NOTICE_FILE_UNSENT
from test_matrix_bot import DM_ROOM, FAMILY_ROOM, FakeTransport, _bot, matrix_config  # noqa: F401 - fixture
from test_matrix_transport import running

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- encryption / content --------------------------------------------------------


def test_encrypted_file_round_trips_through_the_inbound_decryptor() -> None:
    plaintext = b"%PDF-1.7 synthetic report" * 100
    ciphertext, info = outbound_media.encrypt(plaintext)
    assert ciphertext != plaintext and len(ciphertext) == len(plaintext)
    assert info["v"] == "v2" and info["key"]["alg"] == "A256CTR"
    file = {**info, "url": "mxc://example.org/abc"}
    assert inbound_media.decrypt(ciphertext, file) == plaintext


def test_file_content_marks_images_and_files() -> None:
    _cipher, info = outbound_media.encrypt(b"x")
    image = outbound_media.file_content("a.png", "image/png", 1, "mxc://s/1", info)
    doc = outbound_media.file_content("r.pdf", "application/pdf", 1, "mxc://s/2", info)
    assert image["msgtype"] == "m.image" and doc["msgtype"] == "m.file"
    assert doc["file"]["url"] == "mxc://s/2" and doc["body"] == "r.pdf"
    assert "url" not in doc, "plaintext url would bypass encryption"


def test_read_deliverable_is_bounded(tmp_path: Path) -> None:
    small = tmp_path / "a.txt"
    small.write_bytes(b"12345")
    assert outbound_media.read_deliverable(small, max_bytes=5) == b"12345"
    with pytest.raises(outbound_media.OutboundMediaError, match="too-large"):
        outbound_media.read_deliverable(small, max_bytes=4)
    with pytest.raises(outbound_media.OutboundMediaError, match="missing"):
        outbound_media.read_deliverable(tmp_path / "gone.pdf", max_bytes=10)


# --- deliverable rule -----------------------------------------------------------------


def test_deliverable_paths_follow_the_telegram_rule(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    report = tmp_path / "out" / "report.pdf"
    report.write_bytes(b"pdf")
    code = tmp_path / "out" / "app.py"
    code.write_text("print()")
    text = f"see out/report.pdf and {report} again, code at out/app.py, missing out/none.pdf"
    assert deliverables.resolve_deliverable_paths(text, tmp_path, max_bytes=10) == [report.resolve()]
    assert deliverables.resolve_deliverable_paths(text, tmp_path, max_bytes=3) == []


def test_telegram_keeps_the_shared_rule() -> None:
    from telegram_bot.core.bot import TelegramBot

    assert TelegramBot._FILE_PATH_RE is deliverables.FILE_PATH_RE
    assert TelegramBot._SENDABLE_FILE_EXTENSIONS is deliverables.SENDABLE_FILE_EXTENSIONS


# --- MatrixBot queues deliverables ------------------------------------------------------


class FileTransport(FakeTransport):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.files: list[tuple[str, str, str]] = []

    def enqueue_file(self, room_id: str, path: str, *, key: str) -> str:
        self.files.append((room_id, path, key))
        return "$file-x"


def _files(root: Path, count: int) -> list[Path]:
    (root / "out").mkdir(exist_ok=True)
    paths = []
    for i in range(count):
        p = root / "out" / f"r{i}.pdf"
        p.write_bytes(b"pdf")
        paths.append(p)
    return paths


def _attached(tmp_path: Path) -> tuple[Any, FileTransport]:
    bot, _chat, _manager = _bot(tmp_path)
    transport = FileTransport({}, None)
    bot._transport = transport
    bot._active_turn_id = "$turn1"
    return bot, transport


@pytest.mark.usefixtures("matrix_config")
def test_direct_room_answers_queue_their_files_once_per_turn(tmp_path: Path) -> None:
    bot, transport = _attached(tmp_path)
    (a, b) = _files(tmp_path, 2)
    bot._enqueue_deliverables(DM_ROOM, f"done: out/r0.pdf and {b}")
    assert [(room, path) for room, path, _key in transport.files] == [(DM_ROOM, str(a)), (DM_ROOM, str(b))]
    assert [key for *_rest, key in transport.files] == ["deliverable-$turn1-0", "deliverable-$turn1-1"]
    assert transport.notices == []


@pytest.mark.usefixtures("matrix_config")
def test_family_rooms_get_a_notice_instead_of_files(tmp_path: Path) -> None:
    bot, transport = _attached(tmp_path)
    _files(tmp_path, 1)
    bot._enqueue_deliverables(FAMILY_ROOM, "out/r0.pdf")
    assert transport.files == []
    assert transport.notices == [(FAMILY_ROOM, FILES_DIRECT_ONLY.format(count=1))]


@pytest.mark.usefixtures("matrix_config")
def test_outside_root_and_over_cap_files_are_counted_not_sent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    bot, _chat, _manager = _bot(tmp_path, project_root=root)
    transport = FileTransport({}, None)
    bot._transport = transport
    bot._active_turn_id = "$turn2"
    inside = _files(root, MAX_FILES_PER_TURN + 1)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"x")
    bot._enqueue_deliverables(DM_ROOM, " ".join(str(p) for p in [*inside, outside]))
    assert len(transport.files) == MAX_FILES_PER_TURN
    assert transport.notices == [(DM_ROOM, FILES_SKIPPED.format(count=2))]


@pytest.mark.usefixtures("matrix_config")
def test_file_sending_can_be_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_MATRIX_SEND_FILES", "0")
    bot, transport = _attached(tmp_path)
    _files(tmp_path, 1)
    bot._enqueue_deliverables(DM_ROOM, "out/r0.pdf")
    assert transport.files == [] and transport.notices == []


# --- transport delivers a file row ------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self) -> Any:
        return self._body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _http(upload_status: int = 200, config_size: int | None = None) -> tuple[Any, list[tuple[str, str, Any]]]:
    calls: list[tuple[str, str, Any]] = []

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        calls.append((method, url, kwargs.get("data")))
        if url.endswith("/media/config"):
            return _Response(200, {"m.upload.size": config_size} if config_size else {})
        return _Response(upload_status, {"content_uri": "mxc://matrix.example/Up1"})

    return types.SimpleNamespace(request=request, close=AsyncMock()), calls


async def _file_row(h: Any, path: Path) -> dict[str, Any]:
    room = h.f.c["rooms"][0]
    h.f.enqueue_file(room, str(path), key="deliverable-$t-0")
    rows = [row for row in h.f.store.outbox() if row["body"] == FILE_JOB_BODY]
    assert len(rows) == 1
    return rows[0]


async def test_a_file_row_is_encrypted_uploaded_and_sent(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        report = tmp_path / "report.pdf"
        report.write_bytes(b"%PDF synthetic")
        f.http, calls = _http()
        sent: list[tuple[str, str, dict[str, Any], str]] = []

        async def encrypted_raw(room: str, kind: str, content: Any, txn: str) -> str:
            sent.append((room, kind, dict(content), txn))
            return "$sent"

        f._encrypted_raw = encrypted_raw  # type: ignore[method-assign]
        row = await _file_row(h, report)
        assert await f._deliver(row) is True
        upload = [c for c in calls if c[0] == "POST"]
        assert len(upload) == 1 and upload[0][2] != b"%PDF synthetic", "only ciphertext is uploaded"
        (room, kind, content, _txn), = sent
        assert kind == "m.room.message" and content["msgtype"] == "m.file"
        assert content["file"]["url"] == "mxc://matrix.example/Up1" and content["body"] == "report.pdf"
        assert inbound_media.decrypt(upload[0][2], content["file"]) == b"%PDF synthetic"
        assert not [r for r in f.store.outbox() if r["event_id"] == row["event_id"]], "row delivered"


async def test_a_missing_or_refused_file_gets_one_notice_and_never_blocks(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        f.http, _calls = _http(upload_status=413)
        f._encrypted_raw = AsyncMock(return_value="$sent")  # type: ignore[method-assign]
        big = tmp_path / "big.zip"
        big.write_bytes(b"z" * 10)
        row = await _file_row(h, big)
        assert await f._deliver(row) is True
        assert NOTICE_FILE_UNSENT.format(name="big.zip") in h.replies()
        f._encrypted_raw.assert_not_awaited()
        assert f.store.get_meta("outbound_file_failures")["too-large"] == 1

    (tmp_path / "second").mkdir()
    async with running(tmp_path / "second") as h:
        f = h.f
        f.http, _calls = _http()
        gone = tmp_path / "gone.pdf"
        gone.write_bytes(b"x")
        row = await _file_row(h, gone)
        gone.unlink()
        assert await f._deliver(row) is True
        assert NOTICE_FILE_UNSENT.format(name="gone.pdf") in h.replies()


async def test_a_temporary_upload_error_keeps_the_row_for_retry(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        f.http, _calls = _http(upload_status=503)
        report = tmp_path / "r.pdf"
        report.write_bytes(b"x")
        row = await _file_row(h, report)
        with pytest.raises(ConnectionError):
            await f._deliver(row)
        assert [r for r in f.store.outbox() if r["event_id"] == row["event_id"]], "row still ready"


async def test_the_homeserver_upload_limit_lowers_the_cap(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        f.http, _calls = _http(config_size=4)
        f._encrypted_raw = AsyncMock(return_value="$sent")  # type: ignore[method-assign]
        report = tmp_path / "r.pdf"
        report.write_bytes(b"12345")
        row = await _file_row(h, report)
        assert await f._deliver(row) is True
        assert NOTICE_FILE_UNSENT.format(name="r.pdf") in h.replies()


async def test_enqueue_file_refuses_family_rooms_and_relative_paths(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        with pytest.raises(ValueError, match="file-path-not-absolute"):
            f.enqueue_file(room, "relative/r.pdf", key="k")
        f.family_rooms = {room}
        with pytest.raises(ValueError, match="file-room-not-direct"):
            f.enqueue_file(room, str(tmp_path / "r.pdf"), key="k")
        payloads = [json.loads(r["reply"]) for r in f.store.outbox() if r["body"] == FILE_JOB_BODY]
        assert payloads == []
