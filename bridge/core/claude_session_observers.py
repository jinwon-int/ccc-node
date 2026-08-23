"""Optional Claude session observer seams for unsolicited delivery and frames."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import Message

if TYPE_CHECKING:
    from .claude_runtime import SdkFrameObserver, UnsolicitedHandler


# Preserve the established body-free trace channel after this pure move.
logger = logging.getLogger("telegram_bot.core.claude_runtime")


class ClaudeSessionObserversMixin:
    """Own optional unsolicited-delivery and raw-frame observer seams."""

    _sdk_frame_observer: SdkFrameObserver | None
    _unsolicited_handler: UnsolicitedHandler | None

    def set_unsolicited_handler(self, handler: UnsolicitedHandler) -> None:
        """Register the between-turns delivery route (optional seam).

        Mirrors the style of the optional runtime seams project_chat probes
        via ``getattr`` (``set_usage_recorder`` / ``set_turn_attempt_recorder``
        on CodexRuntime): runtimes/sessions without the method keep their
        current behavior. The handler is fail-open — exceptions are logged and
        never break the reader task. Re-registration replaces the route.
        """

        self._unsolicited_handler = handler

    def set_sdk_frame_observer(self, observer: SdkFrameObserver) -> None:
        """Register the raw-SDK-frame observation route (optional seam).

        Same optional-seam style as ``set_unsolicited_handler``: callers
        probe it via ``getattr`` and sessions without it keep their current
        behavior. The observer runs synchronously for every frame the reader
        routes — turn and between-turns flows alike, including frames the
        discard machinery swallows — strictly for observation (the /usage
        usage-snapshot and rate-limit recorders). It is fail-open: exceptions
        are logged and never reach turn processing. Re-registration replaces
        the route.
        """

        self._sdk_frame_observer = observer

    @staticmethod
    def _content_texts(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Mapping):
            texts: list[str] = []
            for item in value.values():
                texts.extend(ClaudeSessionObserversMixin._content_texts(item))
            return texts
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            texts = []
            for item in value:
                texts.extend(ClaudeSessionObserversMixin._content_texts(item))
            return texts
        return []

    def _observe_sdk_frame(self, message: Message) -> None:
        observer = self._sdk_frame_observer
        if observer is None:
            return
        try:
            observer(message)
        except Exception:
            # Observation-only seam: a broken observer must never affect the
            # frame routing that serves turns and unsolicited delivery.
            logger.exception("Claude SDK frame observer failed; frame routing continues")
