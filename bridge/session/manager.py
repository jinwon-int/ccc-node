from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Optional

from telegram_bot.session.store import SessionStore

if TYPE_CHECKING:
    from telegram_bot.utils.config import Settings


class SessionManager:
    VALID_REPLY_MODES = {"text", "voice"}
    VALID_PROVIDERS = {"claude", "codex"}
    DEFAULT_REPLY_MODE = "text"
    LAST_USER_MESSAGE_AT_KEY = "last_user_message_at"

    def __init__(self, store: SessionStore, settings: "Settings"):
        self.store = store
        self.settings = settings

    def validate_storage_path(self) -> None:
        """Validate session storage without creating files or directories."""
        self.store.validate_path()

    def initialize(self) -> None:
        self.store.initialize()

    @classmethod
    def normalize_reply_mode(cls, mode: Optional[str]) -> str:
        normalized = str(mode or cls.DEFAULT_REPLY_MODE).strip().lower()
        if normalized not in cls.VALID_REPLY_MODES:
            return cls.DEFAULT_REPLY_MODE
        return normalized

    @classmethod
    def normalize_provider(cls, provider: Optional[str]) -> str:
        """Interpret provider-less legacy rows as Claude."""
        normalized = str(provider or "claude").strip().lower()
        if normalized not in cls.VALID_PROVIDERS:
            raise ValueError(f"Unsupported session provider: {provider!r}")
        return normalized

    def active_provider(self) -> str:
        return self.normalize_provider(getattr(self.settings, "agent_provider", "claude"))

    async def _ensure_reply_mode(
        self, user_id: int, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        current_mode = session.get("reply_mode")
        normalized_mode = self.normalize_reply_mode(current_mode)
        if current_mode != normalized_mode:
            await self.store.patch(user_id, updates={"reply_mode": normalized_mode})
            session["reply_mode"] = normalized_mode
        return session

    async def list_sessions(self) -> Dict[str, Dict[str, Any]]:
        return await self.store.list_sessions()

    async def get_session_provider(self, user_id: int) -> str:
        """Read the effective provider without lazily persisting other defaults."""
        stored = await self.store.get(user_id)
        if stored is None:
            return self.active_provider()
        return self.normalize_provider(stored.get("provider"))

    async def get_session(self, user_id: int) -> Dict[str, Any]:
        stored = await self.store.get(user_id)
        if stored is None:
            session = {
                "provider": self.active_provider(),
                "reply_mode": self.DEFAULT_REPLY_MODE,
            }
            await self.store.patch(user_id, updates=session)
            return session
        session = stored
        session["provider"] = self.normalize_provider(session.get("provider"))
        return await self._ensure_reply_mode(user_id, session)

    async def align_active_provider(self, user_id: int) -> tuple[Dict[str, Any], bool]:
        """Reset provider-specific state in one atomic patch after a provider change."""
        session = await self.get_session(user_id)
        active_provider = self.active_provider()
        if session["provider"] == active_provider:
            return session, False
        await self.patch_session(
            user_id,
            updates={
                "provider": active_provider,
                "session_id": None,
                "new_session": True,
            },
            remove_fields={"model"},
        )
        session.update(provider=active_provider, session_id=None, new_session=True)
        session.pop("model", None)
        return session, True

    async def update_session(self, user_id: int, data: Dict[str, Any]) -> None:
        payload = dict(data)
        if "provider" in payload:
            payload["provider"] = self.normalize_provider(payload.get("provider"))
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        await self.store.update(user_id, payload)

    async def replace_session(self, user_id: int, data: Dict[str, Any]) -> None:
        """Persist a complete session snapshot, including field removals."""
        payload = dict(data)
        if "provider" in payload:
            payload["provider"] = self.normalize_provider(payload.get("provider"))
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        await self.store.set(user_id, payload)

    async def patch_session(
        self,
        user_id: int,
        *,
        updates: Optional[Mapping[str, Any]] = None,
        remove_fields: Iterable[str] = (),
    ) -> None:
        payload = dict(updates or {})
        if "provider" in payload:
            payload["provider"] = self.normalize_provider(payload.get("provider"))
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        await self.store.patch(user_id, updates=payload, remove_fields=remove_fields)

    async def patch_session_if(
        self,
        user_id: int,
        *,
        expected: Mapping[str, Any],
        updates: Optional[Mapping[str, Any]] = None,
        remove_fields: Iterable[str] = (),
    ) -> bool:
        payload = dict(updates or {})
        if "provider" in payload:
            payload["provider"] = self.normalize_provider(payload.get("provider"))
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        return await self.store.patch_if(
            user_id,
            expected=expected,
            updates=payload,
            remove_fields=remove_fields,
        )

    async def get_reply_mode(self, user_id: int) -> str:
        session = await self.get_session(user_id)
        return self.normalize_reply_mode(session.get("reply_mode"))

    async def set_reply_mode(self, user_id: int, mode: str) -> None:
        await self.update_session(
            user_id, {"reply_mode": self.normalize_reply_mode(mode)}
        )

    async def clear_session(self, user_id: int) -> None:
        await self.store.delete(user_id)

    async def set_pending_question(
        self, user_id: int, question_id: str, question_data: Dict[str, Any]
    ) -> None:
        await self.update_session(
            user_id, {"pending_question": {"id": question_id, **question_data}}
        )

    async def get_pending_question(self, user_id: int) -> Optional[Dict[str, Any]]:
        session = await self.get_session(user_id)
        return session.get("pending_question")

    async def clear_pending_question(self, user_id: int) -> None:
        await self.patch_session(user_id, remove_fields={"pending_question"})

    @staticmethod
    def _normalize_timestamp(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_timestamp(cls, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return cls._normalize_timestamp(parsed)

    def _auto_new_session_interval(self) -> Optional[timedelta]:
        hours = self.settings.auto_new_session_after_hours
        if hours is None:
            return None
        return timedelta(hours=float(hours))

    async def get_last_user_message_at(self, user_id: int) -> Optional[datetime]:
        session = await self.get_session(user_id)
        return self._parse_timestamp(session.get(self.LAST_USER_MESSAGE_AT_KEY))

    async def set_last_user_message_at(
        self, user_id: int, at: Optional[datetime] = None
    ) -> None:
        timestamp = self._normalize_timestamp(at or datetime.now(timezone.utc))
        await self.update_session(
            user_id,
            {self.LAST_USER_MESSAGE_AT_KEY: timestamp.isoformat()},
        )

    async def should_start_new_session(
        self, user_id: int, now: Optional[datetime] = None
    ) -> bool:
        interval = self._auto_new_session_interval()
        if interval is None:
            return False

        last_user_message_at = await self.get_last_user_message_at(user_id)
        if last_user_message_at is None:
            return False

        current_time = self._normalize_timestamp(now or datetime.now(timezone.utc))
        return current_time - last_user_message_at > interval
