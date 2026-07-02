"""Shared helpers for telegram_bot.core.bot."""

import logging
import re

logger = logging.getLogger(__name__)


def enforce_access_control(cfg) -> None:
    """Fail-closed access control guard.

    An empty ``allowed_user_ids`` makes ``_check_user_access()`` allow EVERY
    Telegram user, so an accidentally-unset ALLOWED_USER_IDS would silently open
    the bridge to the whole world. Refuse to start in that case unless the
    operator explicitly opts into an open bridge via ``CCC_REQUIRE_ALLOWLIST=false``.

    Raises:
        SystemExit: when the allowlist is required but empty.
    """
    if getattr(cfg, "require_allowlist", True) and not cfg.allowed_user_ids:
        msg = (
            "Refusing to start: ALLOWED_USER_IDS is empty so the bridge would "
            "accept messages from ANY Telegram user. Set ALLOWED_USER_IDS to the "
            "permitted user IDs, or set CCC_REQUIRE_ALLOWLIST=false to "
            "intentionally run an open bridge."
        )
        logger.error(msg)
        raise SystemExit(msg)


class _PollingRestart(Exception):
    """Signal to restart polling loop."""


def _esc_md2(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    return re.sub(r"([_*\[\]()~`>#+=|{}.!\\-])", r"\\\1", text)
