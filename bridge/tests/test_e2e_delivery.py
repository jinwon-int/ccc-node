"""End-to-end delivery reliability harness (issue #385).

Boots the real bridge composition — ``TelegramBot`` → ``ProjectChatHandler``
over the real ``ClaudeRuntime`` adapter (#584), session store, task ledger,
and dead-session recovery — over a fake Telegram surface and a scripted
Claude SDK client. No network, no live provider, no real bot token.

Three round-trips are executed for real, not simulated:

1. **Solicited**: a user message flows Telegram → bridge → agent turn →
   the reply reaches the Telegram outbound surface.
2. **Unsolicited**: a background wakeup turn arrives on the live session with
   no active turn and its output is still delivered
   (#364 P1 / #601 regression guard — dropped entirely before the seam).
3. **Dead-session recovery**: a terminal task notification persisted in a
   dead session's transcript is delivered on the recovery pass
   (#364 P2 / #372 regression guard).

A negative test re-introduces the old drop-condition and shows the
unsolicited round-trip then fails — the positive test is the tripwire that
catches that regression class.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── Real-generation imports ────────────────────────────────────────────────
# Sibling test modules replace claude_agent_sdk and telegram_bot.core.* in
# sys.modules with attribute-less stubs at import time. This harness needs the
# REAL SDK dataclasses and one self-consistent module generation, so evict any
# stubs and re-import the chain fresh.
_sdk = sys.modules.get("claude_agent_sdk")
if _sdk is not None and getattr(_sdk, "__file__", None) is None:
    for name in list(sys.modules):
        if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
            sys.modules.pop(name, None)
for name in (
    "telegram_bot.core.project_chat",
    "telegram_bot.core.project_chat_history",
    "telegram_bot.core.project_chat_process",
    "telegram_bot.core.project_chat_state",
    "telegram_bot.core.claude_runtime",
    "telegram_bot.core.bot",
):
    sys.modules.pop(name, None)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

project_chat_module = importlib.import_module("telegram_bot.core.project_chat")
ProjectChatHandler = project_chat_module.ProjectChatHandler
claude_runtime_module = importlib.import_module("telegram_bot.core.claude_runtime")
ClaudeRuntime = claude_runtime_module.ClaudeRuntime
bot_module = importlib.import_module("telegram_bot.core.bot")
TelegramBot = bot_module.TelegramBot

from telegram_bot.session.manager import SessionManager  # noqa: E402
from telegram_bot.session.store import SessionStore  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _real_settings(tmp_path: Path):
    sys.modules.pop("telegram_bot.utils.config", None)
    settings_class = importlib.import_module("telegram_bot.utils.config").Settings
    return settings_class.load(
        project_root=tmp_path / "project",
        environ={
            "HOME": str(tmp_path),
            "TELEGRAM_BOT_TOKEN": "123456:e2e-fake-token",
            "ALLOWED_USER_IDS": "7",
        },
        bot_env_file=tmp_path / "missing.env",
    )


class ScriptedSDKClient:
    """Echo agent over the real ClaudeSDKClient surface.

    ``query`` scripts an assistant+result turn; ``emit_turn`` outside a query
    injects an unsolicited wakeup turn exactly like the CLI does after a
    background task completes. The session id is announced at connect the way
    the real CLI's init frame does.
    """

    def __init__(self, options=None) -> None:
        del options
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.queries: list[tuple[str, str]] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True
        self.inbox.put_nowait(
            SystemMessage(subtype="init", data={"session_id": "live-session"})
        )

    async def disconnect(self) -> None:
        self.connected = False

    async def interrupt(self) -> None:
        return None

    async def query(self, message: str, session_id: str = "default") -> None:
        self.queries.append((message, session_id))
        self.emit_turn(f"echo: {message}")

    def emit_turn(self, text: str, session_id: str = "live-session") -> None:
        self.inbox.put_nowait(
            AssistantMessage(
                content=[TextBlock(text=text)], model="e2e-model"
            )
        )
        self.inbox.put_nowait(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=session_id,
                result=text,
            )
        )

    async def receive_messages(self):
        while True:
            yield await self.inbox.get()


class FakeTelegramBotAPI:
    """Outbound Telegram surface: records every message the bridge sends."""

    def __init__(self) -> None:
        self.id = 999
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def send_chat_action(self, *args, **kwargs):
        return True

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, **kwargs):
        return True


class FakeMessage:
    def __init__(self, text: str, chat_id: int = 70) -> None:
        self.text = text
        self.message_id = 1
        self.date = datetime.now(timezone.utc)
        self.reply_to_message = None
        self.quote = None
        self.voice = None
        self.replies: list[str] = []

        async def send_action(action=None, **kwargs):
            return True

        self.chat = SimpleNamespace(id=chat_id, send_action=send_action)

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=500 + len(self.replies))


def _update_for(message: FakeMessage, user_id: int = 7):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=message.chat.id),
        message=message,
        callback_query=None,
    )


class Bridge:
    """One booted bridge over fakes: real bot, chat handler, store, ledger."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = _real_settings(tmp_path)
        self.client = ScriptedSDKClient()
        self.runtime = ClaudeRuntime(
            sdk_client_factory=lambda options: self.client,
        )
        self.handler = ProjectChatHandler(
            settings=self.settings,
            agent_runtime=self.runtime,
        )
        self.manager = SessionManager(
            SessionStore(self.settings.bot_data_dir / "sessions.json"), self.settings
        )
        self.manager.initialize()
        self.bot = TelegramBot(
            settings=self.settings,
            session_manager=self.manager,
            project_chat=self.handler,
        )
        self.tg = FakeTelegramBotAPI()
        self.bot.application = SimpleNamespace(bot=self.tg)

    async def send_user_text(self, text: str) -> FakeMessage:
        message = FakeMessage(text)
        await self.bot._handle_text_message(_update_for(message), context=None)
        return message

    def outbound_texts(self) -> list[str]:
        return [text for _chat, text in self.tg.sent if text]

    async def close(self) -> None:
        await self.handler.close()


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def bridge(tmp_path: Path):
    booted = Bridge(tmp_path)
    try:
        yield booted
    finally:
        await booted.close()


@pytest.mark.anyio
async def test_solicited_round_trip_delivers_reply_to_telegram(bridge: Bridge) -> None:
    message = await bridge.send_user_text("hello bridge")

    await _wait_until(lambda: any("echo: hello bridge" in r for r in message.replies))

    assert bridge.client.queries and bridge.client.queries[0][0].endswith("hello bridge")
    # Exactly one reply carries the turn's answer — no duplicates.
    assert sum("echo: hello bridge" in r for r in message.replies) == 1


@pytest.mark.anyio
async def test_unsolicited_background_turn_reaches_telegram(bridge: Bridge) -> None:
    """#364 P1: a wakeup turn with no pending request must still deliver."""
    message = await bridge.send_user_text("start")
    await _wait_until(lambda: any("echo: start" in r for r in message.replies))

    # The turn is finished; the CLI now wakes the model for a completed
    # background task and the turn's output arrives unsolicited.
    await _wait_until(lambda: not bridge.handler._agent_active_sessions)
    bridge.client.emit_turn("Background task finished: build is green")

    await _wait_until(
        lambda: any(
            "Background task finished: build is green" in text
            for text in bridge.outbound_texts()
        )
    )
    delivered = [
        text
        for text in bridge.outbound_texts()
        if "Background task finished: build is green" in text
    ]
    assert len(delivered) == 1, "unsolicited turn must deliver exactly once"


@pytest.mark.anyio
async def test_reintroduced_drop_condition_is_caught_by_harness(bridge: Bridge) -> None:
    """Negative control: with the old drop behavior back in place, the
    unsolicited round-trip fails — proving the positive test is a tripwire."""
    message = await bridge.send_user_text("start")
    await _wait_until(lambda: any("echo: start" in r for r in message.replies))
    await _wait_until(lambda: not bridge.handler._agent_active_sessions)

    session = bridge.handler._agent_sessions[(7, 70)].session

    async def old_drop_behavior(msg):  # the pre-#601 between-turns behavior
        return None

    session._handle_unsolicited_frame = old_drop_behavior
    bridge.client.emit_turn("Background task finished: dropped")

    with pytest.raises(TimeoutError):
        await _wait_until(
            lambda: any(
                "Background task finished: dropped" in text
                for text in bridge.outbound_texts()
            ),
            timeout=0.3,
        )


@pytest.mark.anyio
async def test_dead_session_notification_recovers_to_telegram(bridge: Bridge) -> None:
    """#364 P2 / #372: a dead session's pending terminal task notification is
    delivered by the recovery pass without any live stream."""
    import json

    conversations = bridge.settings.bot_data_dir / "conversations"
    conversations.mkdir(parents=True, exist_ok=True)
    bridge.handler.conversations_dir = conversations
    notification = (
        "<task-notification>"
        "<task-id>task-9</task-id>"
        "<status>completed</status>"
        "<summary>nightly export finished</summary>"
        "</task-notification>"
    )
    (conversations / "dead-session-9.jsonl").write_text(
        json.dumps(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "sessionId": "dead-session-9",
                "content": notification,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    await bridge.manager.store.set("7:70", {"session_id": "dead-session-9"})

    await bridge.bot._recover_dead_session_notifications(bridge.bot.application)

    delivered = [text for text in bridge.outbound_texts() if "nightly export finished" in text]
    assert len(delivered) == 1
    assert delivered[0].startswith("✅ Background task completed")

    # A second pass is idempotent: the durable marker deduplicates delivery.
    await bridge.bot._recover_dead_session_notifications(bridge.bot.application)
    assert (
        sum("nightly export finished" in text for text in bridge.outbound_texts()) == 1
    )
