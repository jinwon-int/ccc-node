"""Telegram conversation scoping helpers.

The default keeps every sender/chat pair isolated. ``shared-groups`` shares
only inside each group. ``shared-all`` is a broader explicit operator opt-in:
every already-authorized sender/chat routes to one global conversation.
"""

from __future__ import annotations

from typing import Any


SESSION_SCOPE_PER_USER_CHAT = "per-user-chat"
SESSION_SCOPE_SHARED_GROUPS = "shared-groups"
SESSION_SCOPE_SHARED_ALL = "shared-all"
SHARED_GROUP_ACTOR_ID = 0  # Telegram user ids are positive; 0 is a route sentinel.
SHARED_ALL_CHAT_ID = 0  # Telegram private/group chat ids are non-zero.


def normalize_session_scope(value: Any) -> str:
    normalized = str(value or SESSION_SCOPE_PER_USER_CHAT).strip().lower().replace("_", "-")
    if normalized in {SESSION_SCOPE_SHARED_GROUPS, SESSION_SCOPE_SHARED_ALL}:
        return normalized
    return SESSION_SCOPE_PER_USER_CHAT


def is_group_conversation(user_id: int, chat_id: int | None) -> bool:
    return chat_id is not None and chat_id != user_id


def storage_key(scope: Any, user_id: int, chat_id: int | None = None) -> Any:
    """Return the durable SessionStore key for one Telegram conversation."""

    normalized = normalize_session_scope(scope)
    if normalized == SESSION_SCOPE_SHARED_ALL:
        return f"{SHARED_GROUP_ACTOR_ID}:{SHARED_ALL_CHAT_ID}"
    if not is_group_conversation(user_id, chat_id):
        return user_id
    if normalized == SESSION_SCOPE_SHARED_GROUPS:
        return f"{SHARED_GROUP_ACTOR_ID}:{chat_id}"
    return f"{user_id}:{chat_id}"


def stream_key(scope: Any, user_id: int, chat_id: int) -> tuple[int, int]:
    """Return the in-memory queue/lock/provider-stream key."""

    normalized = normalize_session_scope(scope)
    if normalized == SESSION_SCOPE_SHARED_ALL:
        return (SHARED_GROUP_ACTOR_ID, SHARED_ALL_CHAT_ID)
    if (
        is_group_conversation(user_id, chat_id)
        and normalized == SESSION_SCOPE_SHARED_GROUPS
    ):
        return (SHARED_GROUP_ACTOR_ID, chat_id)
    return (user_id, chat_id)


def legacy_storage_keys(scope: Any, user_id: int, chat_id: int) -> tuple[Any, ...]:
    """Ordered first-use migration candidates for a newly scoped row."""

    normalized = normalize_session_scope(scope)
    if normalized == SESSION_SCOPE_SHARED_ALL:
        if is_group_conversation(user_id, chat_id):
            return (
                f"{SHARED_GROUP_ACTOR_ID}:{chat_id}",
                f"{user_id}:{chat_id}",
                user_id,
            )
        return (user_id,)
    if not is_group_conversation(user_id, chat_id):
        return ()
    if normalized == SESSION_SCOPE_SHARED_GROUPS:
        return (f"{user_id}:{chat_id}", user_id)
    return (user_id,)
