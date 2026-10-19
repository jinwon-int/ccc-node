"""「한데」 frontend configuration (#2380 PR-2).

A small private JSON file, the hande counterpart of the Matrix config:

.. code-block:: json

    {
      "account": "@node-a:hande",
      "owner": "@owner:hande",
      "rooms": ["agent"],
      "family_rooms": [],
      "family_users": [],
      "socket_path": "/run/ccc-hande/responder.sock",
      "socket_mode": "0660",
      "socket_group": "native-relay-bot",
      "approval_timeout_s": 120,
      "interim_frames": false,
      "notice_frames": false
    }

Identities are ``@<actor>:hande``: the bot already sends ``@<device>:hande``
and this frontend folds the device into its actor (``owner-pc`` → ``owner``),
so ``owner``/``family_users`` name actors. Rooms are the native relay's room
names (``[A-Za-z0-9_-]{1,64}``). The file must not be group/world readable:
it holds no secret today, but the Matrix rule (``private-config-required``)
is kept so a future field cannot leak by default.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import stat
from typing import Any, Mapping

HANDE_SERVER = "hande"
ACTOR_PATTERN = re.compile(r"[A-Za-z0-9_]{1,32}")
ROOM_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
IDENTITY_PATTERN = re.compile(r"@([A-Za-z0-9_]{1,32}):hande")
DEFAULT_APPROVAL_TIMEOUT_S = 120
MAX_APPROVAL_TIMEOUT_S = 600
DEFAULT_SOCKET_MODE = 0o660


class HandeConfigError(RuntimeError):
    """The hande frontend cannot start because its configuration is unusable."""


def actor_of_device(device: str) -> str:
    """``owner-iphone-27a879`` → ``owner`` — the native bot's own rule (``responder.rs``)."""

    return str(device).split("-", 1)[0]


def identity(actor: str) -> str:
    return f"@{actor}:{HANDE_SERVER}"


def identity_actor(value: str) -> str | None:
    match = IDENTITY_PATTERN.fullmatch(str(value))
    return match.group(1) if match else None


def sender_identity(sender: str) -> str | None:
    """Fold the bot's ``@<device>:hande`` into ``@<actor>:hande``; ``None`` if malformed."""

    match = re.fullmatch(r"@([A-Za-z0-9_-]{1,128}):hande", str(sender))
    if match is None:
        return None
    actor = actor_of_device(match.group(1))
    return identity(actor) if ACTOR_PATTERN.fullmatch(actor) else None


def _require_private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise HandeConfigError(f"hande config must be private (0600): {path}")


def _string_list(config: Mapping[str, Any], key: str) -> list[str]:
    raw = config.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise HandeConfigError(f"hande config {key!r} must be a list of strings")
    return list(raw)


def _validate_identities(config: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    account = config.get("account")
    owner = config.get("owner")
    if not isinstance(account, str) or identity_actor(account) is None:
        raise HandeConfigError("hande config 'account' must be '@<node>:hande'")
    if not isinstance(owner, str) or identity_actor(owner) is None:
        raise HandeConfigError("hande config 'owner' must be '@<actor>:hande'")
    family_users = _string_list(config, "family_users")
    if any(identity_actor(user) is None for user in family_users):
        raise HandeConfigError("hande config 'family_users' entries must be '@<actor>:hande'")
    if account in family_users or account == owner:
        raise HandeConfigError("hande config 'account' is the bot, not a user")
    return account, owner, family_users


def _validate_rooms(config: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    rooms = _string_list(config, "rooms")
    if not rooms or any(not ROOM_PATTERN.fullmatch(room) for room in rooms):
        raise HandeConfigError("hande config 'rooms' must name at least one relay room")
    if len(set(rooms)) != len(rooms):
        raise HandeConfigError("hande config 'rooms' repeats a room")
    family_rooms = _string_list(config, "family_rooms")
    if any(room not in rooms for room in family_rooms):
        raise HandeConfigError("hande config 'family_rooms' must be a subset of 'rooms'")
    return rooms, family_rooms


def _validate_socket(config: Mapping[str, Any]) -> tuple[str | None, int, str | None]:
    socket_path = config.get("socket_path")
    if socket_path is not None and (not isinstance(socket_path, str) or not socket_path.strip()):
        raise HandeConfigError("hande config 'socket_path' must be a non-empty path")
    mode_raw = config.get("socket_mode", "0660")
    try:
        socket_mode = int(str(mode_raw), 8)
    except ValueError as error:
        raise HandeConfigError("hande config 'socket_mode' must be octal like '0660'") from error
    if socket_mode & 0o007:
        raise HandeConfigError("hande config 'socket_mode' must not grant 'other'")
    group = config.get("socket_group")
    if group is not None and (not isinstance(group, str) or not group):
        raise HandeConfigError("hande config 'socket_group' must be a group name")
    return socket_path, socket_mode, group


def _validate_knobs(config: Mapping[str, Any]) -> dict[str, Any]:
    timeout_raw = config.get("approval_timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)
    if type(timeout_raw) is not int or not 1 <= timeout_raw <= MAX_APPROVAL_TIMEOUT_S:
        raise HandeConfigError(
            f"hande config 'approval_timeout_s' must be 1..{MAX_APPROVAL_TIMEOUT_S}"
        )
    knobs: dict[str, Any] = {"approval_timeout_s": timeout_raw}
    for key in ("interim_frames", "notice_frames"):
        value = config.get(key, False)
        if type(value) is not bool:
            raise HandeConfigError(f"hande config {key!r} must be true or false")
        knobs[key] = value
    return knobs


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalised copy or raise :class:`HandeConfigError` (fail closed)."""

    if not isinstance(config, Mapping):
        raise HandeConfigError("hande config must be a JSON object")
    account, owner, family_users = _validate_identities(config)
    rooms, family_rooms = _validate_rooms(config)
    socket_path, socket_mode, group = _validate_socket(config)
    return {
        "account": account,
        "owner": owner,
        "rooms": rooms,
        "family_rooms": family_rooms,
        "family_users": family_users,
        "socket_path": socket_path,
        "socket_mode": socket_mode,
        "socket_group": group,
        **_validate_knobs(config),
    }


def load_config(path: Path | str) -> dict[str, Any]:
    """Read and validate the private hande config file."""

    file = Path(path).expanduser()
    if not file.is_file():
        raise HandeConfigError(f"hande config file not found: {file}")
    _require_private(file)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HandeConfigError(f"hande config unreadable: {type(error).__name__}") from error
    return validate_config(raw)
