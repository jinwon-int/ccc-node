"""Stable integer identities for Matrix users and rooms (#1780).

``ProjectChatHandler`` and everything below it key conversations by
``(user_id: int, chat_id: int)`` and rely on two Telegram facts:

* ids are positive and never ``0`` (``session_scope`` uses ``0`` as a route
  sentinel), and
* a direct chat has ``chat_id == user_id`` (``is_group_conversation``).

A Matrix frontend therefore maps ``@user:server`` and ``!room:server`` onto
positive ints and, for direct rooms, reports the *sender's* int as the chat
id. The map is persisted (private file) so ints stay stable across restarts —
the reverse lookup is what outbound sends and async completions need.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

MATRIX_USER_RE = re.compile(r"@[^:\s]{1,255}:[A-Za-z0-9.\-\[\]:]{1,255}")
MATRIX_ROOM_RE = re.compile(r"![^:\s]{1,255}:[A-Za-z0-9.\-\[\]:]{1,255}")
_ID_BITS = 52  # fits a JSON/JS-safe integer; leaves headroom below 2**53
_MIN_ID = 1_000_000_000_000  # never collides with real Telegram ids or the 0 sentinel


def _derive(kind: str, matrix_id: str, attempt: int) -> int:
    digest = hashlib.sha256(f"{kind}\0{matrix_id}\0{attempt}".encode()).digest()
    value = int.from_bytes(digest[:8], "big") & ((1 << _ID_BITS) - 1)
    return _MIN_ID + (value % ((1 << _ID_BITS) - _MIN_ID))


class MatrixIdMap:
    """Bidirectional, persisted ``matrix id <-> int`` map.

    ``path`` is created 0600 on first write. Corrupt or foreign content fails
    closed (``ValueError``) instead of silently re-numbering conversations.
    """

    def __init__(self, path: Path | str | None) -> None:
        self._path = Path(path) if path is not None else None
        self._forward: dict[str, int] = {}
        self._reverse: dict[int, str] = {}
        if self._path is not None and self._path.exists():
            self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"matrix id map unreadable: {self._path}") from exc
        entries = raw.get("ids") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            raise ValueError(f"matrix id map malformed: {self._path}")
        for matrix_id, value in entries.items():
            if not isinstance(matrix_id, str) or not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"matrix id map malformed: {self._path}")
            if value < _MIN_ID or value in self._reverse:
                raise ValueError(f"matrix id map malformed: {self._path}")
            self._forward[matrix_id] = value
            self._reverse[value] = matrix_id

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps({"version": 1, "ids": self._forward}, ensure_ascii=True, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self._path)

    # -- lookups -------------------------------------------------------------

    def _intern(self, kind: str, matrix_id: str) -> int:
        existing = self._forward.get(matrix_id)
        if existing is not None:
            return existing
        for attempt in range(64):
            candidate = _derive(kind, matrix_id, attempt)
            if candidate not in self._reverse:
                self._forward[matrix_id] = candidate
                self._reverse[candidate] = matrix_id
                self._save()
                return candidate
        raise RuntimeError("matrix id map exhausted collision retries")

    def user_id(self, matrix_user: str) -> int:
        if not isinstance(matrix_user, str) or not MATRIX_USER_RE.fullmatch(matrix_user):
            raise ValueError("invalid matrix user id")
        return self._intern("user", matrix_user)

    def room_id(self, matrix_room: str) -> int:
        if not isinstance(matrix_room, str) or not MATRIX_ROOM_RE.fullmatch(matrix_room):
            raise ValueError("invalid matrix room id")
        return self._intern("room", matrix_room)

    def chat_id(self, matrix_room: str, sender: str, *, direct: bool) -> int:
        """Return the handler ``chat_id`` for one message.

        Direct rooms report the sender's int so ``is_group_conversation`` is
        false (private memory/session scope); every other room gets its own int.
        """

        if direct:
            return self.user_id(sender)
        return self.room_id(matrix_room)

    def matrix_id(self, value: int) -> str | None:
        """Reverse lookup for outbound delivery; ``None`` when unknown."""

        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return self._reverse.get(value)

    def snapshot(self) -> dict[str, Any]:
        return {"count": len(self._forward), "path": str(self._path) if self._path else None}
