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


# --- fan-out (CCC_PUSH_MIRROR_DIRS / CCC_PUSH_CONSUME_SPOOL) ---------------


@pytest.mark.anyio
async def test_fan_out_mirrors_record_before_delivery(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror-telegram"
    settings = _settings(tmp_path, push_mirror_dirs=str(mirror))
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    data = {"event": "AgentCronRun", "text": "laptop ONLINE", "dedup": "k"}
    (tmp_path / "spool" / "sent").mkdir(parents=True, exist_ok=True)
    p = _record(tmp_path, "a.json", data)
    raw = p.read_text(encoding="utf-8")
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert len(enqueued) == 1, "primary channel still delivers"
    assert (mirror / "a.json").read_text(encoding="utf-8") == raw, "byte-identical copy"
    assert (tmp_path / "spool" / "sent" / "a.json").exists()
    assert not list(mirror.glob(".*.tmp")), "no temp file left behind"


@pytest.mark.anyio
async def test_fan_out_is_idempotent_across_retries(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    settings = _settings(tmp_path, push_mirror_dirs=str(mirror), push_max_per_minute=1)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    notifier._sent_times.append(time.time())  # rate-limited: record is kept
    _record(tmp_path, "a.json", {"event": "x", "text": "one"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert enqueued == [] and (tmp_path / "spool" / "a.json").exists()
    assert (mirror / "a.json").exists(), "mirrored even while the primary defers"
    # The mirror's consumer already delivered and archived it; the retry must
    # not re-queue it there.
    (mirror / "sent").mkdir()
    (mirror / "a.json").rename(mirror / "sent" / "a.json")
    notifier._sent_times.clear()
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert len(enqueued) == 1
    assert not (mirror / "a.json").exists(), "not mirrored a second time"


@pytest.mark.anyio
async def test_fan_out_failure_keeps_record_and_skips_delivery(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # mkdir under a file fails
    settings = _settings(tmp_path, push_mirror_dirs=str(blocker / "mirror"))
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    _record(tmp_path, "a.json", {"event": "x", "text": "t"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert enqueued == [], "no channel gets it until every channel can"
    assert (tmp_path / "spool" / "a.json").exists(), "kept for retry"


@pytest.mark.anyio
async def test_malformed_and_empty_records_are_not_mirrored(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    settings = _settings(tmp_path, push_mirror_dirs=str(mirror))
    t, _ = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    (tmp_path / "spool" / "bad.json").write_text("{not json", encoding="utf-8")
    _record(tmp_path, "empty.json", {"event": "x", "text": " "})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert not mirror.exists() or not list(mirror.glob("*.json"))


def test_consume_spool_overrides_drained_dir(tmp_path: Path) -> None:
    t, _ = _transport({DIRECT: "direct"})
    n = MatrixSpoolNotifier(
        _settings(tmp_path, push_consume_spool_dir=str(tmp_path / "in")), t
    )
    assert n.spool_dir == tmp_path / "in"
    n2 = MatrixSpoolNotifier(_settings(tmp_path, push_consume_spool_dir=None), t)
    assert n2.spool_dir == tmp_path / "spool"


def test_mirror_that_feeds_this_consumer_is_ignored(tmp_path: Path) -> None:
    t, _ = _transport({DIRECT: "direct"})
    spool = tmp_path / "spool"
    other = tmp_path / "other"
    n = MatrixSpoolNotifier(
        _settings(tmp_path, push_mirror_dirs=f"{spool},{other},{other}"), t
    )
    assert n.mirror_dirs == [other], "self and duplicates dropped"


@pytest.mark.anyio
async def test_fan_out_is_not_blocked_by_primary_send_failure(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"

    class _Down:
        policy = SimpleNamespace(rooms={DIRECT: "direct"})

        def enqueue_notice(self, room: str, text: str) -> None:
            raise RuntimeError("homeserver down")

    notifier = MatrixSpoolNotifier(_settings(tmp_path, push_mirror_dirs=str(mirror)), _Down())
    for i in range(5):
        _record(tmp_path, f"r{i}.json", {"event": "x", "text": f"t{i}"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert sorted(p.name for p in mirror.glob("*.json")) == [f"r{i}.json" for i in range(5)]
    assert len(list((tmp_path / "spool").glob("*.json"))) == 5, "primary keeps all for retry"


@pytest.mark.anyio
async def test_fan_out_is_not_throttled_by_primary_rate_limit(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    settings = _settings(tmp_path, push_mirror_dirs=str(mirror), push_max_per_minute=2)
    t, enqueued = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(settings, t)
    (tmp_path / "spool" / "sent").mkdir(parents=True, exist_ok=True)
    for i in range(6):
        _record(tmp_path, f"r{i}.json", {"event": "x", "text": f"t{i}"})
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert len(enqueued) == 2
    assert len(list(mirror.glob("*.json"))) == 6


@pytest.mark.anyio
async def test_stale_fan_out_temp_files_are_swept(tmp_path: Path) -> None:
    import os

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    old = mirror / ".x.json.1.tmp"
    fresh = mirror / ".y.json.2.tmp"
    old.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    past = time.time() - 2 * 60 * 60
    os.utime(old, (past, past))
    t, _ = _transport({DIRECT: "direct"})
    notifier = MatrixSpoolNotifier(_settings(tmp_path, push_mirror_dirs=str(mirror)), t)
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    await notifier._drain(DIRECT, tmp_path / "spool" / "sent")
    assert not old.exists() and fresh.exists(), "only crash leftovers older than an hour"
