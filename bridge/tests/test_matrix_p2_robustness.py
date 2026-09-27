"""Matrix frontend robustness P2 bundle (#1959).

Retry backoff that never reset and ignored a 429's requested wait, id/room map
writes without fsync, nio/aiohttp log records unfiltered on the default
frontend, the matrix sub-package missing from the wheel, and a duplicated
/skills dispatch path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import tomllib
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core import matrix_ids
from telegram_bot.core.matrix import bot as matrix_bot
from telegram_bot.core.matrix import transport as t
from telegram_bot.core.matrix.transport import MatrixTemporaryError, MatrixTransport
from test_matrix_bot import OWNER, FakeSink, _bot, _job
from test_matrix_bot import matrix_config as _shared  # noqa: F401 - fixture registration below

BRIDGE = Path(__file__).resolve().parents[1]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


# --- retry: backoff reset + server-requested wait ------------------------------------


def _retry_self() -> Any:
    store = SimpleNamespace(set_meta=lambda *a, **k: None)
    return SimpleNamespace(store=store, leg_failures={"receive": 0}, leg_error={"receive": ""})


@pytest.mark.anyio
async def test_retry_resets_after_a_healthy_run_and_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(t, "_HEALTHY_RUN_S", 0.05)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    attempts = {"n": 0}

    async def operation() -> None:
        attempts["n"] += 1
        n = attempts["n"]
        if n <= 3:
            raise ConnectionError("matrix-temporary-error")  # fast failures: 1, 2, 4
        if n == 4:
            # A long healthy run (the sync loop) that eventually drops.
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 0.08)
            except TimeoutError:
                pass
            raise ConnectionError("matrix-temporary-error")
        if n == 5:
            raise MatrixTemporaryError(retry_after=7.0)  # server asks for more than the backoff
        return None

    monkeypatch.setattr(t.asyncio, "sleep", fake_sleep)
    await MatrixTransport.retry(_retry_self(), operation, leg="receive")
    assert sleeps == [1.0, 2.0, 4.0, 1.0, 7.0]


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        yield self._body[:size]


def _response(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(status=status, headers=headers or {}, content=_Content(body))


@pytest.mark.anyio
async def test_retry_after_parsing_is_bounded_and_fail_soft() -> None:
    parse = MatrixTransport._retry_after
    assert await parse(_response(429, json.dumps({"retry_after_ms": 2500}).encode())) == 2.5
    assert await parse(_response(503, headers={"Retry-After": "12"})) == 12.0
    assert await parse(_response(429, json.dumps({"retry_after_ms": 10_000_000}).encode())) == t._RETRY_AFTER_CAP_S
    assert await parse(_response(429, b"{not json")) is None
    assert await parse(_response(429, json.dumps({"retry_after_ms": True}).encode())) is None
    assert await parse(_response(429, json.dumps({"retry_after_ms": -5}).encode())) is None
    assert await parse(_response(502)) is None
    err = MatrixTemporaryError(3.0)
    assert isinstance(err, ConnectionError) and str(err) == "matrix-temporary-error" and err.retry_after == 3.0
    assert t.retry_label(err) == "matrix-temporary-error"  # health label unchanged


# --- durable id / direct-room maps -----------------------------------------------------


def test_id_and_direct_room_maps_fsync_before_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_fsync = matrix_ids.os.fsync

    def fsync(fd: int) -> None:
        calls.append("file")
        real_fsync(fd)

    monkeypatch.setattr(matrix_ids.os, "fsync", fsync)
    monkeypatch.setattr(matrix_ids, "_fsync_directory", lambda path: calls.append("dir:" + Path(path).name))
    monkeypatch.setattr(matrix_bot, "_fsync_directory", lambda path: calls.append("dir:" + Path(path).name))

    ids = matrix_ids.MatrixIdMap(tmp_path / "ids" / "matrix-ids.json")
    ids.user_id("@owner:example.org")
    assert calls == ["file", "dir:ids"]
    assert json.loads((tmp_path / "ids" / "matrix-ids.json").read_text())["ids"]

    calls.clear()
    rooms = matrix_bot._DirectRoomMap(tmp_path / "rooms" / "matrix-direct-rooms.json")
    rooms.remember("@owner:example.org", "!dm:example.org")
    assert calls == ["file", "dir:rooms"]
    assert rooms.room_for("@owner:example.org") == "!dm:example.org"


# --- transport log filter on the default frontend ------------------------------------------


def test_transport_log_filter_withholds_client_records_once(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler()
    monkeypatch.setattr(root, "handlers", [handler])
    matrix_bot._install_transport_log_filter()
    matrix_bot._install_transport_log_filter()  # idempotent
    filters = [f for f in handler.filters if isinstance(f, matrix_bot._TransportLogFilter)]
    assert len(filters) == 1

    leaky = logging.LogRecord("nio.client", logging.WARNING, __file__, 1, "GET %s", ("https://hs/?access_token=SECRET",), None)
    assert filters[0].filter(leaky) is True
    assert leaky.getMessage() == matrix_bot._TRANSPORT_LOG_WITHHELD
    own = logging.LogRecord("telegram_bot.core.matrix.bot", logging.INFO, __file__, 1, "turn %s", ("ok",), None)
    filters[0].filter(own)
    assert own.getMessage() == "turn ok"


# --- packaging -----------------------------------------------------------------------------


def test_every_core_subpackage_is_listed_for_the_wheel() -> None:
    packages = set(tomllib.loads((BRIDGE / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"])
    subpackages = {
        "telegram_bot.core." + init.parent.name
        for init in (BRIDGE / "core").glob("*/__init__.py")
        if init.parent.name != "__pycache__"
    }
    assert "telegram_bot.core.matrix" in subpackages
    assert subpackages <= packages


# --- /skills uses the shared dispatch path, still on provider defaults ----------------------


@pytest.mark.anyio
async def test_skills_ignores_the_stored_model_and_starts_fresh(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager = _bot(tmp_path)
    manager.rows[bot.ids.user_id(OWNER)] = {
        "provider": manager.provider, "session_id": "s-old", "model": "gpt-5-mini", "effort": "high",
    }
    await bot.run_turn(_job("/skills"), sink=FakeSink(), session_id=None, room_kind="direct")
    (call,) = chat.calls
    assert call["new_session"] is True and call["session_id"] is None
    assert call["model"] is None and call["effort"] is None
    assert call["usage_mode"] == "interactive"
