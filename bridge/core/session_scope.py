"""Telegram conversation scoping helpers.

The default keeps every sender/chat pair isolated.  ``shared-groups`` is an
explicit operator opt-in: DMs remain per-user while every allowlisted sender in
the same group uses one storage row, queue, lock, and provider stream.
"""

from __future__ import annotations

from typing import Any


SESSION_SCOPE_PER_USER_CHAT = "per-user-chat"
SESSION_SCOPE_SHARED_GROUPS = "shared-groups"
SHARED_GROUP_ACTOR_ID = 0  # Telegram user ids are positive; 0 is a route sentinel.


def normalize_session_scope(value: Any) -> str:
    normalized = str(value or SESSION_SCOPE_PER_USER_CHAT).strip().lower().replace("_", "-")
    if normalized == SESSION_SCOPE_SHARED_GROUPS:
        return SESSION_SCOPE_SHARED_GROUPS
    return SESSION_SCOPE_PER_USER_CHAT


def is_group_conversation(user_id: int, chat_id: int | None) -> bool:
    return chat_id is not None and chat_id != user_id


def storage_key(scope: Any, user_id: int, chat_id: int | None = None) -> Any:
    """Return the durable SessionStore key for one Telegram conversation."""

    if not is_group_conversation(user_id, chat_id):
        return user_id
    if normalize_session_scope(scope) == SESSION_SCOPE_SHARED_GROUPS:
        return f"{SHARED_GROUP_ACTOR_ID}:{chat_id}"
    return f"{user_id}:{chat_id}"


def stream_key(scope: Any, user_id: int, chat_id: int) -> tuple[int, int]:
    """Return the in-memory queue/lock/provider-stream key."""

    if (
        is_group_conversation(user_id, chat_id)
        and normalize_session_scope(scope) == SESSION_SCOPE_SHARED_GROUPS
    ):
        return (SHARED_GROUP_ACTOR_ID, chat_id)
    return (user_id, chat_id)


def legacy_storage_keys(scope: Any, user_id: int, chat_id: int) -> tuple[Any, ...]:
    """Ordered first-use migration candidates for a newly scoped row."""

    if not is_group_conversation(user_id, chat_id):
        return ()
    if normalize_session_scope(scope) == SESSION_SCOPE_SHARED_GROUPS:
        return (f"{user_id}:{chat_id}", user_id)
    return (user_id,)
