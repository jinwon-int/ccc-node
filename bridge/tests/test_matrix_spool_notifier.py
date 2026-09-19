"""MatrixSpoolNotifier — channel-neutral push spool consumed by the matrix frontend."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.matrix.bot import MatrixSpoolNotifier
from telegram_bot.core.push_notifier import PushNotifier


def _settings(tmp_path: Path, **over):
    base = dict(
        push_enabled=True,
        push_spool_dir=str(tmp_path / "spool"),
        push_poll_interval=0.01,
        push_max_per_minute=10,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _transport(rooms: dict[str, str]):
    enqueued: list[tuple[str, str]] = []

    class _T:
        policy = SimpleNamespace(rooms=rooms)

        def enqueue_notice(self, room: str, text: str) -> None:
            enqueued.append((room, text))

    return _T(), enqueued


def _record(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / "spool" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


DIRECT = "!direct:hs"
FAMILY = "!family:hs"


def test_owner_room_prefers_direct_then_family() -> None:
    t, _ = _transport({DIRECT: "direct", FAMILY: "mention"})
    assert MatrixSpoolNotifier(_settings(Path("/tmp")), t)._owner_room() == DIRECT
    t2, _ = _transport({FAMILY: "mention"})
    assert MatrixSpoolNotifier(_settings(Path("/tmp")), t2)._owner_room() == FAMILY
    t3, _ = _transport({})
    assert MatrixSpoolNotifier(_settings(Path("/tmp")), t3)._owner_room() is None


@pytest.mark.anyio
async def test_drain_delivers_formatted_record_and_archives(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    data = {"event": "SelfUpdate", "node": "jingun", "text": "업데이트 완료", "dedup": "SelfUpdate:x", "ts": "T"}
    (tmp_path / "spool" / "sent").mkdir(parents=True, exist_ok=True)
    _record(tmp_path, "a.json", data)
    await notifier._drain(DIRECT, Path(settings.push_spool_dir) / "sent")
    assert enqueued == [(DIRECT, PushNotifier._format(data))]
    assert not list((tmp_path / "spool").glob("*.json"))
    assert (tmp_path / "spool" / "sent" / "a.json").exists()


@pytest.mark.anyio
async def test_drain_archives_malformed_and_empty_without_enqueue(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    (tmp_path / "spool" / "bad.json").write_text("{not json", encoding="utf-8")
    _record(tmp_path, "empty.json", {"event": "x", "text": "   "})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert enqueued == []
    assert not list((tmp_path / "spool").glob("*.json"))


@pytest.mark.anyio
async def test_drain_dedups_within_window(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    import time

    data = {"event": "SelfUpdate", "text": "hello", "dedup": "SelfUpdate:same"}
    notifier._recent["SelfUpdate:same"] = time.time()
    _record(tmp_path, "a.json", data)
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert enqueued == []  # same dedup key inside the window: archived silently


@pytest.mark.anyio
async def test_drain_rate_limit_defers(tmp_path: Path) -> None:
    settings = _settings(tmp_path, push_max_per_minute=1)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    notifier._sent_times.append(time.time())
    _record(tmp_path, "a.json", {"event": "x", "text": "one"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert enqueued == []
    assert (tmp_path / "spool" / "a.json").exists(), "deferred: file kept for the next cycle"


@pytest.mark.anyio
async def test_drain_archives_poison_and_retries_transient(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    enqueued: list[tuple[str, str]] = []

    class _Flaky:
        policy = SimpleNamespace(rooms={DIRECT: "direct"})

        def __init__(self, fail: Exception) -> None:
            self.fail = fail

        def enqueue_notice(self, room: str, text: str) -> None:
            raise self.fail

    notifier = MatrixSpoolNotifier(settings, _Flaky(ValueError("room-not-allowed")))
    _record(tmp_path, "poison.json", {"event": "x", "text": "t"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert enqueued == [] and not (tmp_path / "spool" / "poison.json").exists(), "permanent: archived"

    notifier2 = MatrixSpoolNotifier(settings, _Flaky(RuntimeError("homeserver down")))
    _record(tmp_path, "retry.json", {"event": "x", "text": "t"})
    await notifier2._drain(DIRECT, tmp_path / "spool" / "sent")
    assert (tmp_path / "spool" / "retry.json").exists(), "transient: kept for retry"


@pytest.mark.anyio
async def test_disabled_notifier_returns_without_touching_spool(tmp_path: Path) -> None:
    settings = _settings(tmp_path, push_enabled=False)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    await asyncio.wait_for(notifier.run(), timeout=1)
    assert enqueued == []
