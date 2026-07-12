from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Optional

from telegram_bot.session.store import SessionStore

if TYPE_CHECKING:
    from telegram_bot.utils.config import Settings


class SessionManager:
    VALID_REPLY_MODES = {"text", "voice"}
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

    async def get_session(self, user_id: int) -> Dict[str, Any]:
        session = await self.store.get(user_id) or {}
        return await self._ensure_reply_mode(user_id, session)

    async def update_session(self, user_id: int, data: Dict[str, Any]) -> None:
        payload = dict(data)
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        await self.store.update(user_id, payload)

    async def replace_session(self, user_id: int, data: Dict[str, Any]) -> None:
        """Persist a complete session snapshot, including field removals."""
        payload = dict(data)
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
