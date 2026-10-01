"""Per-conversation external-wait status message (#2081): renderer, store, plan, Telegram sync."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import telegram.error

from telegram_bot.core import bot_wait_status
from telegram_bot.core.bot_wait_status import BotWaitStatusMixin
from telegram_bot.core.external_wait import (
    STATE_MONITORING,
    TERMINAL_EXPIRED,
    TERMINAL_FAILURE,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    default_registry_path,
)
from telegram_bot.core.external_wait_monitor import ExternalWaitMonitor, PrState
from telegram_bot.core.external_wait_status import (
    MAX_STATUS_LINES,
    ExternalWaitStatusStore,
    _duration,
    default_status_store_path,
    plan_status_update,
    records_for_route,
    render_wait_status,
    text_hash_of,
)
from telegram_bot.utils.redaction import REDACTION_MARKER


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_KST = timezone(timedelta(hours=9))
NOW = 1_700_000_000.0


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=_KST).strftime("%H:%M")


def _wait(
    wait_id: str = "w1",
    *,
    pr: int = 598,
    state: str = STATE_MONITORING,
    created: float = NOW - 600,
    timeout: float = 6 * 3600,
    completed: float | None = None,
    summary: str = "squash-merge the PR",
    user_id: int = 7,
    chat_id: int = 70,
) -> dict:
    rec = {
        "wait_id": wait_id,
        "repo": "jinwon-int/ccc-node",
        "pr_number": pr,
        "head_sha": "abc1234",
        "user_id": user_id,
        "chat_id": chat_id,
        "summary": summary,
        "state": state,
        "created_epoch": created,
        "expires_epoch": created + timeout,
        "terminal_status": None if state == STATE_MONITORING else state,
        "completed_epoch": completed,
    }
    return rec


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_monitoring_only_is_one_line_per_wait() -> None:
    text = render_wait_status([_wait()], NOW)
    assert text == (
        f"⏳ Waiting for results · PR #598 CI — squash-merge the PR "
        f"(registered {_hhmm(NOW - 600)}, up to 6h)"
    )


def test_render_without_summary_omits_the_dash() -> None:
    text = render_wait_status([_wait(summary="")], NOW)
    assert text is not None
    assert " — " not in text and "PR #598 CI (registered" in text


def test_render_mixed_keeps_chronological_order_and_terminal_wording() -> None:
    done = _wait("w0", pr=1, state=TERMINAL_SUCCESS, created=NOW - 900, completed=NOW - 60)
    text = render_wait_status([_wait(created=NOW - 600), done], NOW)
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == f"✅ CI green → continuing · PR #1 CI ({_hhmm(NOW - 60)})"
    assert lines[1].startswith("⏳ Waiting for results · PR #598 CI")


@pytest.mark.parametrize(
    ("status", "wording"),
    [
        (TERMINAL_FAILURE, "❌ CI failed → investigating"),
        ("cancelled", "⚠️ CI cancelled"),
        ("superseded", "🔀 head moved"),
        (TERMINAL_EXPIRED, "⏰ expired"),
        ("monitor-error", "⚠️ CI watch failed"),
        ("owner-cancel", "🚫 cancelled by owner"),
        ("something-new", "ℹ️ CI watch ended"),
    ],
)
def test_render_all_terminal_recent_uses_headline_style(status: str, wording: str) -> None:
    text = render_wait_status([_wait(state=status, completed=NOW - 10)], NOW)
    assert text is not None and text.startswith(wording) and "⏳" not in text


def test_render_all_terminal_stale_is_none() -> None:
    stale = _wait(state=TERMINAL_SUCCESS, completed=NOW - 1801)
    assert render_wait_status([stale], NOW) is None
    assert render_wait_status([stale], NOW, recent_terminal_seconds=3600) is not None
    assert render_wait_status([], NOW) is None
    # A terminal record without a completion stamp is never "recent".
    assert render_wait_status([_wait(state=TERMINAL_SUCCESS, completed=None)], NOW) is None


def test_render_redacts_a_token_like_summary() -> None:
    token = "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
    text = render_wait_status([_wait(summary=f"push with {token}")], NOW)
    assert text is not None
    assert token not in text and REDACTION_MARKER in text


def test_render_truncates_long_summaries_and_caps_lines() -> None:
    long_summary = "x" * 150
    text = render_wait_status([_wait(summary=long_summary)], NOW)
    assert text is not None and "…" in text and long_summary not in text
    many = [_wait(f"w{i}", pr=i, created=NOW - 1000 + i) for i in range(MAX_STATUS_LINES + 3)]
    text = render_wait_status(many, NOW)
    assert text is not None
    lines = text.splitlines()
    assert len(lines) == MAX_STATUS_LINES
    assert lines[-1] == "… and 4 more"
    assert "PR #0 CI" in lines[0]


def test_duration_formatting() -> None:
    assert _duration(30) == "1m"
    assert _duration(90) == "1m"
    assert _duration(3600) == "1h"
    assert _duration(5400) == "1h30m"
    assert _duration(6 * 3600) == "6h"


def test_records_for_route_filters_and_orders() -> None:
    records = [
        _wait("late", created=NOW - 10),
        _wait("other", user_id=8),
        _wait("early", created=NOW - 500),
        {"wait_id": "bad", "user_id": "x", "chat_id": None},
    ]
    assert [r["wait_id"] for r in records_for_route(records, 7, 70)] == ["early", "late"]
    assert records_for_route(records, 9, 70) == []


def test_registry_records_for_route(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    common = {"head_sha": "abc1234", "session_id": None, "summary": "", "timeout_seconds": 100, "poll_interval_seconds": 30}
    registry.register(repo="o/r", pr_number=2, user_id=7, chat_id=70, now=NOW + 5, **common)
    registry.register(repo="o/r", pr_number=1, user_id=7, chat_id=70, now=NOW, **common)
    registry.register(repo="o/r", pr_number=3, user_id=7, chat_id=71, now=NOW, **common)
    assert [r["pr_number"] for r in registry.records_for_route(7, 70)] == [1, 2]
    assert registry.records_for_route(7, 72) == []


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_round_trip(tmp_path: Path) -> None:
    store = ExternalWaitStatusStore(default_status_store_path(tmp_path / "external-wait"), clock=lambda: NOW)
    assert store.get(7, 70) is None
    assert store.pop(7, 70) is None
    store.put(7, 70, message_id=42, text_hash="abcd")
    entry = store.get(7, 70)
    assert entry == {"message_id": 42, "chat_id": 70, "user_id": 7, "updated_epoch": NOW, "text_hash": "abcd"}
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert list(raw) == ["7:70"]
    store.put(7, 70, message_id=43, text_hash="ef01", now=NOW + 1)
    assert store.entries()["7:70"]["message_id"] == 43
    assert store.pop(7, 70)["message_id"] == 43
    assert store.get(7, 70) is None and store.entries() == {}


def test_store_corrupt_file_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "status-messages.json"
    path.write_text("{not json", encoding="utf-8")
    store = ExternalWaitStatusStore(path)
    assert store.get(7, 70) is None and store.entries() == {}
    store.put(7, 70, message_id=1, text_hash="h")
    assert store.get(7, 70)["message_id"] == 1
    path.write_text('["a list", 1]', encoding="utf-8")
    assert store.entries() == {}
    path.write_text('{"7:70": "not a dict", "8:80": {"message_id": 2}}', encoding="utf-8")
    assert list(store.entries()) == ["8:80"]
    path.write_text("", encoding="utf-8")
    assert store.entries() == {}


def test_store_write_failure_is_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExternalWaitStatusStore(tmp_path / "status-messages.json")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("telegram_bot.core.external_wait_status._atomic_write_bytes", boom)
    store.put(7, 70, message_id=1, text_hash="h")
    assert store.get(7, 70) is None
    assert store.pop(7, 70) is None


# ---------------------------------------------------------------------------
# plan_status_update
# ---------------------------------------------------------------------------


def test_reconcile_routes_puts_stored_first_and_skips_malformed() -> None:
    from telegram_bot.core.external_wait_status import reconcile_routes

    entries = {"8:80": {"user_id": 8, "chat_id": 80}, "junk": {"user_id": "x"}}
    records = [
        _wait("w1", user_id=7, chat_id=70),
        _wait("w2", user_id=8, chat_id=80),  # already covered by its stored entry
        _wait("w3", state=TERMINAL_SUCCESS, user_id=9, chat_id=90, completed=NOW),
        {**_wait("w4"), "user_id": None},
    ]
    assert reconcile_routes(entries, records) == [(8, 80), (7, 70)]


def test_plan_matrix() -> None:
    monitoring = [_wait()]
    recent = [_wait(state=TERMINAL_SUCCESS, completed=NOW - 5)]
    stale = [_wait(state=TERMINAL_SUCCESS, completed=NOW - 5000)]
    text = render_wait_status(monitoring, NOW)
    assert text is not None
    stored = {"message_id": 1, "text_hash": text_hash_of(text)}

    assert plan_status_update([], None, NOW) == ("noop", None)
    assert plan_status_update(monitoring, None, NOW) == ("send", text)
    # Already finished and no message: the wake notification covered it.
    assert plan_status_update(recent, None, NOW) == ("noop", None)
    assert plan_status_update(stale, None, NOW) == ("noop", None)
    assert plan_status_update(monitoring, stored, NOW) == ("noop", text)
    action, final = plan_status_update(recent, stored, NOW)
    assert action == "edit" and final is not None and final.startswith("✅")
    assert plan_status_update(stale, stored, NOW) == ("delete", None)
    assert plan_status_update([], stored, NOW) == ("delete", None)
    assert plan_status_update(monitoring, {"message_id": 1, "text_hash": "old"}, NOW) == ("edit", text)


# ---------------------------------------------------------------------------
# Telegram sync
# ---------------------------------------------------------------------------


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[dict] = []
        self.edit_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.send_returns_id = True
        self._next_id = 100

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self._next_id += 1
        if not self.send_returns_id:
            return SimpleNamespace()
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        if self.edit_error is not None:
            raise self.edit_error
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return True


class _StatusBot(BotWaitStatusMixin):
    pass


def _bot(tmp_path: Path, fake: FakeBot | None = None) -> tuple[_StatusBot, FakeBot, ExternalWaitRegistry, ExternalWaitStatusStore]:
    fake = fake or FakeBot()
    bot = _StatusBot()
    bot._config = SimpleNamespace(bot_data_dir=tmp_path, project_root=str(tmp_path))  # type: ignore[assignment]
    bot.application = SimpleNamespace(bot=fake)
    home = tmp_path / "external-wait"
    return bot, fake, ExternalWaitRegistry(default_registry_path(home)), ExternalWaitStatusStore(default_status_store_path(home))


def _register(registry: ExternalWaitRegistry, pr: int = 598, *, user_id: int = 7, chat_id: int = 70, now: float | None = None) -> str:
    return registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=pr,
        head_sha="abc1234",
        user_id=user_id,
        chat_id=chat_id,
        session_id="sess-1",
        summary="squash-merge the PR",
        timeout_seconds=6 * 3600,
        poll_interval_seconds=30,
        now=time.time() if now is None else now,
    )


@pytest.mark.anyio
async def test_sync_sends_silently_then_edits_then_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    # No waits, no stored message: silence.
    await bot._sync_external_wait_status(7, 70)
    assert fake.sent == []

    wait_id = _register(registry)
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.sent) == 1
    assert fake.sent[0]["chat_id"] == 70 and fake.sent[0]["disable_notification"] is True
    assert fake.sent[0]["text"].startswith("⏳ Waiting for results · PR #598 CI")
    stored = store.get(7, 70)
    assert stored is not None and stored["message_id"] == 101
    assert stored["text_hash"] == text_hash_of(fake.sent[0]["text"])

    # Same state again: no Telegram call at all.
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.sent) == 1 and fake.edited == []

    # Terminal transition: the same message is edited in place.
    registry.finish(wait_id, TERMINAL_SUCCESS)
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.sent) == 1 and len(fake.edited) == 1
    assert fake.edited[0]["message_id"] == 101 and fake.edited[0]["chat_id"] == 70
    assert fake.edited[0]["text"].startswith("✅ CI green → continuing · PR #598 CI")
    assert store.get(7, 70)["text_hash"] == text_hash_of(fake.edited[0]["text"])

    # Thirty-one minutes later nothing recent is left: delete and forget.
    monkeypatch.setattr(bot_wait_status, "time", SimpleNamespace(time=lambda: time.time() + 31 * 60))
    await bot._sync_external_wait_status(7, 70)
    assert fake.deleted == [{"chat_id": 70, "message_id": 101}]
    assert store.get(7, 70) is None
    # And the route is silent again.
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.sent) == 1 and len(fake.deleted) == 1


@pytest.mark.anyio
async def test_sync_not_modified_is_swallowed_and_hash_refreshed(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    _register(registry)
    store.put(7, 70, message_id=5, text_hash="stale-hash")
    fake.edit_error = telegram.error.BadRequest("Message is not modified")
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.edited) == 1 and fake.sent == []
    assert store.get(7, 70)["message_id"] == 5
    assert store.get(7, 70)["text_hash"] == text_hash_of(fake.edited[0]["text"])


@pytest.mark.anyio
async def test_sync_edit_not_found_resends_while_monitoring(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    _register(registry)
    store.put(7, 70, message_id=5, text_hash="stale-hash")
    fake.edit_error = telegram.error.BadRequest("Message to edit not found")
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.edited) == 1 and len(fake.sent) == 1
    assert store.get(7, 70)["message_id"] == 101


@pytest.mark.anyio
async def test_sync_edit_not_found_without_monitoring_just_forgets(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    wait_id = _register(registry)
    registry.finish(wait_id, TERMINAL_FAILURE)
    store.put(7, 70, message_id=5, text_hash="stale-hash")
    fake.edit_error = telegram.error.BadRequest("Message to edit not found")
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.edited) == 1 and fake.sent == []
    assert store.get(7, 70) is None


@pytest.mark.anyio
async def test_sync_other_edit_errors_keep_the_stored_id(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    _register(registry)
    store.put(7, 70, message_id=5, text_hash="stale-hash")
    fake.edit_error = telegram.error.BadRequest("Chat_id is empty")
    await bot._sync_external_wait_status(7, 70)
    assert store.get(7, 70)["message_id"] == 5 and store.get(7, 70)["text_hash"] == "stale-hash"
    fake.edit_error = RuntimeError("boom")
    await bot._sync_external_wait_status(7, 70)
    assert store.get(7, 70)["text_hash"] == "stale-hash" and fake.sent == []


@pytest.mark.anyio
async def test_sync_delete_failures(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    store.put(7, 70, message_id=5, text_hash="h")
    fake.delete_error = RuntimeError("network down")
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.deleted) == 1 and store.get(7, 70) is not None  # retried next sync
    fake.delete_error = telegram.error.BadRequest("Message to delete not found")
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.deleted) == 2 and store.get(7, 70) is None


@pytest.mark.anyio
async def test_sync_send_without_message_id_stores_nothing(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    fake.send_returns_id = False
    _register(registry)
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.sent) == 1 and store.get(7, 70) is None


@pytest.mark.anyio
async def test_sync_is_gated_by_env_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    _register(registry)
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_STATUS", "false")
    assert bot._external_wait_status_syncer() is None
    await bot._sync_external_wait_status(7, 70)
    await bot._reconcile_external_wait_status_on_start()
    assert fake.sent == []
    monkeypatch.delenv("CCC_EXTERNAL_WAIT_STATUS")
    assert bot._external_wait_status_syncer() is not None


@pytest.mark.anyio
async def test_sync_swallows_registry_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, fake, registry, store = _bot(tmp_path)

    def boom(self):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(_StatusBot, "_external_wait_status_registry", boom)
    await bot._sync_external_wait_status(7, 70)
    await bot._reconcile_external_wait_status_on_start()
    assert fake.sent == [] and fake.edited == [] and fake.deleted == []


@pytest.mark.anyio
async def test_startup_reconcile_refreshes_finalizes_and_drops(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    # Route A: monitoring wait whose message was never sent (died before turn end).
    _register(registry, pr=1, user_id=7, chat_id=70)
    # Route B: stored message, wait finished while the bridge was down -> final line.
    done = _register(registry, pr=2, user_id=8, chat_id=80)
    registry.finish(done, TERMINAL_FAILURE)
    store.put(8, 80, message_id=20, text_hash="old")
    # Route C: stored message, nothing left (pruned/stale) -> delete.
    store.put(9, 90, message_id=30, text_hash="old")
    # Route D: stored message already current -> untouched.
    _register(registry, pr=4, user_id=10, chat_id=100)
    current = render_wait_status(registry.records_for_route(10, 100), time.time())
    assert current is not None
    store.put(10, 100, message_id=40, text_hash=text_hash_of(current))
    # Malformed store entry is skipped.
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["junk"] = {"message_id": 1, "user_id": "x"}
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    await bot._reconcile_external_wait_status_on_start()

    assert [m["chat_id"] for m in fake.sent] == [70]
    assert [m["message_id"] for m in fake.edited] == [20]
    assert fake.edited[0]["text"].startswith("❌ CI failed → investigating · PR #2 CI")
    assert fake.deleted == [{"chat_id": 90, "message_id": 30}]
    assert store.get(9, 90) is None and store.get(10, 100)["message_id"] == 40
    assert store.get(7, 70)["message_id"] == 101


@pytest.mark.anyio
async def test_startup_reconcile_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    monkeypatch.setattr(bot_wait_status, "MAX_RECONCILE_ROUTES", 2)
    for chat in (70, 71, 72):
        store.put(7, chat, message_id=chat, text_hash="old")
    await bot._reconcile_external_wait_status_on_start()
    assert len(fake.deleted) == 2


@pytest.mark.anyio
async def test_startup_reconcile_with_nothing_is_silent(tmp_path: Path) -> None:
    bot, fake, registry, store = _bot(tmp_path)
    await bot._reconcile_external_wait_status_on_start()
    assert fake.sent == [] and fake.deleted == []


# ---------------------------------------------------------------------------
# Monitor hook + lifecycle composition
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, start: float = 1_100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _Transport:
    def __init__(self, state: PrState) -> None:
        self.state = state

    async def fetch_pr_state(self, repo: str, pr_number: int) -> PrState:
        return self.state


async def _notify(chat_id: int, text: str) -> bool:
    return True


async def _resume(record: dict, prompt: str) -> bool:
    return True


@pytest.mark.anyio
async def test_monitor_calls_status_syncer_on_terminal_transition(tmp_path: Path) -> None:
    clock = _Clock()
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    wait_id = _register(registry, now=1_000.0)
    synced: list[tuple[int, int]] = []

    async def syncer(user_id: int, chat_id: int) -> None:
        synced.append((user_id, chat_id))

    monitor = ExternalWaitMonitor(
        registry,
        transport=_Transport(PrState(head_sha="abc1234", rollup="success")),
        notifier=_notify,
        resumer=_resume,
        status_syncer=syncer,
        clock=clock,
    )
    await monitor._tick()
    assert synced == [(7, 70)]
    assert registry.get(wait_id)["wake"]["state"] == "done"
    # Nothing left to deliver: the syncer is not called again.
    await monitor._tick()
    assert synced == [(7, 70)]


@pytest.mark.anyio
async def test_monitor_syncer_runs_on_skipped_wakes_and_failures_are_swallowed(tmp_path: Path) -> None:
    clock = _Clock()
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    wait_id = _register(registry, now=1_000.0)
    calls: list[tuple[int, int]] = []

    async def syncer(user_id: int, chat_id: int) -> None:
        calls.append((user_id, chat_id))
        raise RuntimeError("status down")

    monitor = ExternalWaitMonitor(
        registry,
        transport=_Transport(PrState(head_sha="abc1234", rollup="pending")),
        notifier=_notify,
        resumer=None,
        status_syncer=syncer,
        clock=clock,
    )
    clock.now = 1_000.0 + 7 * 3600  # past the registered timeout -> expired, resume skipped
    await monitor._tick()
    assert calls == [(7, 70)]
    assert registry.get(wait_id)["terminal_status"] == TERMINAL_EXPIRED
    assert registry.get(wait_id)["wake"]["state"] == "done"


@pytest.mark.anyio
async def test_monitor_without_syncer_is_unchanged(tmp_path: Path) -> None:
    clock = _Clock()
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    wait_id = _register(registry, now=1_000.0)
    monitor = ExternalWaitMonitor(
        registry,
        transport=_Transport(PrState(head_sha="abc1234", rollup="failure")),
        notifier=_notify,
        resumer=_resume,
        clock=clock,
    )
    await monitor._tick()
    assert registry.get(wait_id)["wake"]["state"] == "done"


async def _session_of(value):
    return value


def _composed_lifecycle(tmp_path: Path, fake: FakeBot):
    from telegram_bot.core import bot_delivery, bot_lifecycle

    class _Composed(bot_lifecycle.BotLifecycleMixin, bot_delivery.BotDeliveryMixin, BotWaitStatusMixin):
        pass

    bot = _Composed()
    bot._config = SimpleNamespace(bot_data_dir=tmp_path, project_root=str(tmp_path))  # type: ignore[assignment]
    bot._session_manager = SimpleNamespace(  # type: ignore[assignment]
        get_session=lambda user_id: _session_of({"session_id": "sess-1"})
    )
    bot._project_chat = SimpleNamespace()  # type: ignore[assignment]
    bot.application = SimpleNamespace(bot=fake)
    return bot


@pytest.mark.anyio
async def test_lifecycle_monitor_carries_the_status_syncer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _composed_lifecycle(tmp_path, FakeBot())
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None and monitor._status_syncer is not None
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_STATUS", "0")
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None and monitor._status_syncer is None


@pytest.mark.anyio
async def test_lifecycle_resume_refreshes_status_after_its_reply(tmp_path: Path) -> None:
    fake = FakeBot()
    bot = _composed_lifecycle(tmp_path, fake)
    registry = ExternalWaitRegistry(default_registry_path(tmp_path / "external-wait"))

    async def process_message(prompt, user_id, chat_id, **kwargs):
        # The continuation registers a chained CI wait before replying.
        _register(registry, pr=599, user_id=user_id, chat_id=chat_id)
        return SimpleNamespace(success=True, content="pushed; waiting for CI", streamed=False)

    bot._project_chat = SimpleNamespace(process_message=process_message)  # type: ignore[assignment]
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None
    record = {
        "wait_id": "w1", "user_id": 7, "chat_id": 70, "session_id": "sess-1",
        "repo": "jinwon-int/ccc-node", "pr_number": 598, "head_sha": "abc1234",
        "terminal_status": TERMINAL_SUCCESS, "summary": "merge",
    }
    assert await monitor._run_resume(record) is True
    texts = [m["text"] for m in fake.sent]
    assert texts[0] == "pushed; waiting for CI"
    assert texts[-1].startswith("⏳ Waiting for results · PR #599 CI")
    assert fake.sent[-1]["disable_notification"] is True


@pytest.mark.anyio
async def test_lifecycle_refresh_is_a_noop_without_the_mixin(tmp_path: Path) -> None:
    from telegram_bot.core import bot_lifecycle

    class _LifecycleOnly(bot_lifecycle.BotLifecycleMixin):
        pass

    await _LifecycleOnly()._refresh_external_wait_status(7, 70)
