"""Admission policy, private state and configuration for the Matrix transport.

Port of the ``family-messenger`` pilot's ``fleet_core`` and
``fleet_matrix_state`` modules (plus ``fleet_matrix.family_config``). No
network, provider invocation, or device-trust decisions live here; the
transport must authenticate/decrypt events before setting ``decrypted=True``.
Each account owns one private directory and one process for the lifetime of
:class:`Store`.

Differences from the pilot (#1780 PR-2a):

* There is no subprocess worker, so ``worker_argv`` / ``worker_argv_family`` /
  ``remote_worker`` are neither required nor part of the saved policy. A saved
  policy written by the pilot that still carries those keys is upgraded
  silently (the keys are ignored); every other policy difference still fails
  closed with ``saved-policy-changed``.
* The pilot's ``worker_cleanup_unconfirmed`` / ``worker_cleanup_in_progress``
  meta flags are gone with the subprocess. The only block that survives a
  restart is *uncertain work*: a turn whose outcome could not be confirmed
  (crash, timeout, cancellation, runner exception). Uncertain jobs are never
  re-run automatically; the transport pauses all new work until each one is
  acknowledged with ``/ack <turn id>`` in its room, or cleared by an operator
  through :meth:`MatrixStore.unblock`, which records who/when/why in
  ``operator_audit``. :data:`BLOCK_KEYS` therefore names the fields of
  :meth:`MatrixStore.block` rather than meta keys.
* :meth:`Store.finish` accepts an empty reply: the turn is recorded as done
  without an outbox delivery (the runner already streamed or had nothing to
  say).
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

MAX_TEXT_BYTES = 16_384
MAX_REPLY_BYTES = 65_536

BLOCK_KEYS: tuple[str, ...] = ("blocked_scopes", "uncertain_scopes")
SCOPE_PATTERN = re.compile(r"[0-9a-f]{64}")
ACCOUNT_PATTERN = re.compile(r"@[a-zA-Z0-9._=-]+:[a-zA-Z0-9.:-]+")
DEVICE_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,64}")
KEY_PATTERN = re.compile(r"[A-Za-z0-9+/]{43}")

OPERATOR_ACK_TEXT = "운영자가 이전 작업의 결과 확인을 완료한 것으로 기록했습니다. 자동 재실행은 하지 않습니다."

#: Every fail-closed reason the state module and the transport can raise.
SAFETY_STOP_REASONS: frozenset[str] = frozenset(
    {
        # configuration
        "private-config-required",
        "invalid-config",
        "missing-config-fields",
        "invalid-homeserver",
        "invalid-account",
        "distinct-bot-required",
        "invalid-device",
        "strong-pickle-key-required",
        "invalid-state-path",
        "invalid-room-list",
        "pin-owner-devices",
        "invalid-device-pin",
        "invalid-key-pin",
        "invalid-family_rooms",
        "invalid-family_users",
        "invalid-family-rooms",
        "invalid-family-users",
        "invalid-family-devices",
        "invalid-family-device-pin",
        "invalid-family-notice-text",
        "invalid-mention-aliases",
        "invalid-wake-words",
        "invalid-identities",
        "owner-identity-changed",
        "family-identity-changed",
        "cross-signing-missing",
        "cross-signing-invalid",
        "saved-policy-changed",
        # state
        "pilot-storage-limit",
        "pending-sync-not-finished",
        "invalid-sync",
        "sync-too-large",
        "sync-identity-mismatch",
        "control-identity-conflict",
        "notice-identity-conflict",
        # transport
        "matrix-http-",  # prefix; the HTTP status is appended
        "matrix-response-too-large",
        "unsafe-crypto-store",
        "explicit-new-device-initialization-required",
        "credential-device-mismatch",
        "crypto-identity-or-token-drift",
        "key-upload-failed",
        "device-query-failed",
        "owner-device-set-changed",
        "unexpected-agent-device",
        "published-agent-key-changed",
        "owner-device-key-changed",
        "pinned-device-missing",
        "pinned-device-key-changed",
        "private-room-membership-changed",
        "encrypted-room-required",
        "sdk-encryption-state-missing",
        "sdk-room-membership-changed",
        "timeline-gap-requires-backfill",
        "invalid-sync-response",
        "unverified-owner-event",
        "unverified-family-event",
        "plaintext-output-refused",
    }
)


class SafetyStop(RuntimeError):
    """Needs operator reconciliation; do not hide gaps or reset cryptographic identity."""


class QueueFull(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #


def handle_pattern(name: str) -> re.Pattern[str]:
    """Whole-token ``@name`` in message text, case-insensitive."""
    return re.compile(r"(?<![\w.@-])@" + re.escape(name) + r"(?![\w.:-])", re.IGNORECASE)


def HANDLE_RE(account: str) -> re.Pattern[str]:  # name kept from the pilot
    """Whole-token @localpart of a Matrix account, case-insensitive (e.g. @fambot for @fambot:hs)."""
    return handle_pattern(account[1:].split(":", 1)[0])


# Korean particles (longest first) that may be glued to a wake word without a
# space — "서서야뭐해" is 서서 + 야 + rest. A trailing character that is Hangul
# but not one of these particles still blocks the match, so "서서히" stays inert.
WAKE_JOSA = (
    "에게서", "한테서", "부터", "까지", "에서", "에게", "한테", "이랑", "처럼", "만큼",
    "보다", "조차", "라도", "으로", "님", "은", "는", "이", "가", "을",
    "를", "와", "과", "도", "만", "랑", "에", "게", "로", "야", "아", "여",
)


def wake_word_pattern(word: str) -> re.Pattern[str]:
    """Whole-token bare ``word`` (no leading @) in message text, case-insensitive.

    Matches the word alone (followed by a space, punctuation, or end), or the
    word glued to a Korean particle with the rest unconstrained — no-space
    typing like "서서야뭐해" still addresses the bot.
    """
    josa = "|".join(WAKE_JOSA)
    return re.compile(
        r"(?<!\w)" + re.escape(word) + r"(?:(?![가-힣])|(?:" + josa + r"))",
        re.IGNORECASE,
    )


ALIAS_PATTERN = re.compile(r"[a-z0-9._=-]{1,64}")
MAX_MENTION_ALIASES = 8
WAKE_WORD_PATTERN = re.compile(r"[0-9A-Za-z가-힣]{1,64}")
MAX_WAKE_WORDS = 8


def mention_aliases(config: Mapping[str, Any]) -> frozenset[str]:
    """Optional ``mention_aliases``: extra typed handles (e.g. ``@seoseo``) that address the bot.

    Aliases only widen the family-room *mention* gate; sender/room admission
    is unchanged. Matrix user ids cannot be renamed, so this is how a bot
    account keeps its id while the family calls it by its display name.
    """
    raw = config.get("mention_aliases", [])
    if (
        not isinstance(raw, list)
        or len(raw) > MAX_MENTION_ALIASES
        or any(not isinstance(a, str) or not ALIAS_PATTERN.fullmatch(a) for a in raw)
        or len(set(raw)) != len(raw)
    ):
        raise SafetyStop("invalid-mention-aliases")
    return frozenset(raw)


def wake_words(config: Mapping[str, Any]) -> frozenset[str]:
    """Optional ``wake_words``: bare nickname tokens (e.g. 서서, 서서야) that address the bot.

    Like ``mention_aliases`` this only widens the family-room *mention* gate;
    sender and room admission are unchanged. Wake words match as whole tokens
    **without** a leading ``@`` — Korean nicknames have no romanized handle,
    so this is how a family calls the bot by name (owner request 2026-09-18).
    """
    raw = config.get("wake_words", [])
    if (
        not isinstance(raw, list)
        or len(raw) > MAX_WAKE_WORDS
        or any(not isinstance(w, str) or not WAKE_WORD_PATTERN.fullmatch(w) for w in raw)
        or len(set(raw)) != len(raw)
    ):
        raise SafetyStop("invalid-wake-words")
    return frozenset(raw)


def identifier(value: Any, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and 1 < len(value) <= 255
        and not any(ord(c) < 33 for c in value)
    )


def bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("invalid text")
    try:
        if len(value.encode("utf-8")) > limit:
            raise ValueError("text too large")
    except UnicodeError:
        raise ValueError("invalid text encoding") from None
    return value


def scope_of(account: str, room_id: str, sender: str) -> str:
    return hashlib.sha256(json.dumps([account, room_id, sender]).encode()).hexdigest()


@dataclass(frozen=True)
class Request:
    event_id: str
    room_id: str
    sender: str
    body: str
    scope: str


@dataclass(frozen=True)
class Policy:
    account: str
    users: frozenset[str]
    bots: frozenset[str]
    rooms: Mapping[str, str]
    not_before_ms: int
    aliases: frozenset[str] = frozenset()  # extra typed @handles that address the bot
    wake_words: frozenset[str] = frozenset()  # extra bare nickname tokens that address the bot

    def __post_init__(self) -> None:
        users = frozenset(self.users)
        bots = frozenset(self.bots)
        rooms = dict(self.rooms)
        aliases = frozenset(self.aliases)
        wake = frozenset(self.wake_words)
        invalid = (
            not identifier(self.account, "@")
            or not users
            or not rooms
            or self.account not in bots
            or bool(users & bots)
            or any(not identifier(u, "@") for u in users | bots)
            or any(
                not identifier(r, "!") or mode not in {"direct", "mention"}
                for r, mode in rooms.items()
            )
            or type(self.not_before_ms) is not int
            or self.not_before_ms < 0
            or any(not isinstance(a, str) or not ALIAS_PATTERN.fullmatch(a) for a in aliases)
            or any(not isinstance(w, str) or not WAKE_WORD_PATTERN.fullmatch(w) for w in wake)
        )
        if invalid:
            raise ValueError("invalid route policy")
        object.__setattr__(self, "users", users)
        object.__setattr__(self, "bots", bots)
        object.__setattr__(self, "rooms", MappingProxyType(rooms))
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "wake_words", wake)

    def admit(self, room_id: str, event: Any, *, decrypted: bool, now_ms: int) -> Request | None:
        """Reject plaintext, edits, bots, old events and unaddressed group messages."""
        if decrypted is not True or room_id not in self.rooms or not isinstance(event, dict):
            return None
        sender = event.get("sender")
        if not isinstance(sender, str) or sender not in self.users or sender in self.bots:
            return None
        stamp = event.get("origin_server_ts")
        if (
            event.get("type") != "m.room.message"
            or not identifier(event.get("event_id"), "$")
            or type(stamp) is not int
            or type(now_ms) is not int
            or stamp < max(self.not_before_ms, now_ms - 86_400_000)
            or stamp > now_ms + 60_000
        ):
            return None
        content = event.get("content")
        if not isinstance(content, dict) or content.get("msgtype") != "m.text":
            return None
        relation = content.get("m.relates_to", {})
        if not isinstance(relation, dict) or "rel_type" in relation:
            return None
        try:
            body = bounded_text(content.get("body"), MAX_TEXT_BYTES)
        except ValueError:
            return None
        if self.rooms[room_id] == "mention" and not self.addressed(content, body):
            return None
        return Request(event["event_id"], room_id, sender, body, scope_of(self.account, room_id, sender))

    def addressed(self, content: Mapping[str, Any], body: str) -> bool:
        """Family-room gate: spec'd m.mentions, or a typed @localpart handle in the body.

        Element X only emits m.mentions for pill mentions; family members on
        the phone type "@fambot" as plain text (owner request 2026-09-17).
        The handle must match the bot's own localpart as a whole token —
        display names and partial matches still do not count.
        """
        mentions = content.get("m.mentions", {})
        if isinstance(mentions, dict):
            ids = mentions.get("user_ids", [])
            if isinstance(ids, list) and self.account in ids:
                return True
        if HANDLE_RE(self.account).search(body):
            return True
        if any(handle_pattern(alias).search(body) for alias in self.aliases):
            return True
        return any(wake_word_pattern(word).search(body) for word in self.wake_words)


# --------------------------------------------------------------------------- #
# Private files
# --------------------------------------------------------------------------- #


def private_directory(path: Path | str) -> int:
    """Open every component without following links; return a pinned directory fd."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("state directory must be an absolute path without traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(path.parts[1:]):
            if i == len(path.parts) - 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise ValueError("state directory must be owned by this user with mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def private_file(directory_fd: int, name: str) -> int:
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            raise ValueError("invalid state file")
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

REQUIRED_CONFIG_FIELDS = frozenset(
    {
        "homeserver",
        "account",
        "device_id",
        "access_token",
        "pickle_key",
        "state_directory",
        "owner",
        "rooms",
        "devices",
        "not_before_ms",
    }
)
#: Configuration keys that shaped the pilot's saved policy but have no meaning here.
LEGACY_POLICY_KEYS = ("worker_argv", "worker_argv_family", "remote_worker")
POLICY_KEYS = ("owner", "rooms", "devices", "not_before_ms")


def load_config(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    directory = private_directory(path.parent)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_nlink != 1
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_size > 65_536
        ):
            raise SafetyStop("private-config-required")
        data = os.read(fd, 65_537)
        return validate_config(json.loads(data))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _valid_homeserver(config: Mapping[str, Any]) -> bool:
    url = urlsplit(config["homeserver"])
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        return False
    if not url.hostname:
        return False
    if url.scheme == "https":
        return True
    return bool(
        config.get("preview") is True
        and url.scheme == "http"
        and url.hostname in ("127.0.0.1", "localhost")
    )


def identities(config: Mapping[str, Any]) -> dict[str, str]:
    """Optional ``identities``: ``{user_id: {"master": <ed25519 master key>}}`` (#149).

    A user listed here is trusted by **cross-signing** instead of a pinned
    device set: the master key is the only pinned value, and every device the
    user's self-signing key has signed is trusted automatically. Logging in,
    logging out or deleting a device therefore never stops the service; only
    a changed master key (account reset) does. Users without an identity keep
    the pinned-device rule.
    """
    raw = config.get("identities", {})
    if not isinstance(raw, dict) or len(raw) > 13:
        raise SafetyStop("invalid-identities")
    allowed = {config.get("owner")} | set(config.get("family_users") or [])
    cleaned: dict[str, str] = {}
    for user, entry in raw.items():
        if (
            not isinstance(user, str)
            or user not in allowed
            or not isinstance(entry, dict)
            or not isinstance(entry.get("master"), str)
            or not KEY_PATTERN.fullmatch(entry["master"])
        ):
            raise SafetyStop("invalid-identities")
        cleaned[user] = entry["master"]
    return cleaned


def _validate_device_pins(pins: Any, *, missing: str, bad_device: str, bad_key: str) -> None:
    if not isinstance(pins, dict) or not 1 <= len(pins) <= 10:
        raise SafetyStop(missing)
    for device, keys in pins.items():
        if not isinstance(device, str) or not DEVICE_PATTERN.fullmatch(device) or not isinstance(keys, dict):
            raise SafetyStop(bad_device)
        for kind in ("ed25519", "curve25519"):
            if not isinstance(keys.get(kind), str) or not KEY_PATTERN.fullmatch(keys[kind]):
                raise SafetyStop(bad_key)


def validate_config(c: Any) -> dict[str, Any]:
    if not isinstance(c, dict):
        raise SafetyStop("invalid-config")
    if REQUIRED_CONFIG_FIELDS - c.keys():
        raise SafetyStop("missing-config-fields")
    if not isinstance(c["homeserver"], str) or not _valid_homeserver(c):
        raise SafetyStop("invalid-homeserver")
    for key in ("account", "owner"):
        if not isinstance(c[key], str) or not ACCOUNT_PATTERN.fullmatch(c[key]):
            raise SafetyStop("invalid-account")
    if c["account"] == c["owner"]:
        raise SafetyStop("distinct-bot-required")
    if not isinstance(c["device_id"], str) or not DEVICE_PATTERN.fullmatch(c["device_id"]):
        raise SafetyStop("invalid-device")
    for key in ("access_token", "pickle_key"):
        bounded_text(c[key], 8192)
    if len(c["pickle_key"]) < 24:
        raise SafetyStop("strong-pickle-key-required")
    if not isinstance(c["state_directory"], str):
        raise SafetyStop("invalid-state-path")
    directory = Path(c["state_directory"])
    if not directory.is_absolute() or ".." in directory.parts:
        raise SafetyStop("invalid-state-path")
    if not isinstance(c["rooms"], list) or not 1 <= len(c["rooms"]) <= 12:
        raise SafetyStop("invalid-room-list")
    Policy(
        c["account"],
        frozenset([c["owner"]]),
        frozenset([c["account"]]),
        {r: "direct" for r in c["rooms"]},
        c["not_before_ms"],
    )
    owner_identity = c["owner"] in identities(c)
    if not (owner_identity and c["devices"] == {}):
        _validate_device_pins(
            c["devices"],
            missing="pin-owner-devices",
            bad_device="invalid-device-pin",
            bad_key="invalid-key-pin",
        )
    return c


def family_config(
    config: Mapping[str, Any],
) -> tuple[frozenset[str], frozenset[str], dict[str, dict[str, dict[str, str]]]]:
    """Validate optional family-room settings; absent settings stay direct-only."""
    for key in ("family_rooms", "family_users"):
        value = config.get(key, [])
        if (
            not isinstance(value, list)
            or len(value) > 12
            or any(not isinstance(entry, str) or not entry for entry in value)
            or len(set(value)) != len(value)
        ):
            raise SafetyStop("invalid-" + key)
    rooms = config.get("family_rooms", [])
    if any(room not in config["rooms"] for room in rooms):
        raise SafetyStop("invalid-family-rooms")
    users = config.get("family_users", [])
    if (
        config["account"] in users
        or (rooms and not users)
        or any(not ACCOUNT_PATTERN.fullmatch(user) for user in users)
    ):
        raise SafetyStop("invalid-family-users")
    pins = config.get("family_devices", {})
    if not isinstance(pins, dict) or config["owner"] in pins or any(user not in users for user in pins):
        raise SafetyStop("invalid-family-devices")
    total = 0
    cleaned: dict[str, dict[str, dict[str, str]]] = {}
    for user, devices in pins.items():
        _validate_device_pins(
            devices,
            missing="invalid-family-devices",
            bad_device="invalid-family-device-pin",
            bad_key="invalid-family-device-pin",
        )
        entries = {
            device: {"ed25519": keys["ed25519"], "curve25519": keys["curve25519"]}
            for device, keys in devices.items()
        }
        total += len(entries)
        cleaned[user] = entries
    if total > 32:
        raise SafetyStop("invalid-family-devices")
    text = config.get("family_notice_text")
    if text is not None:
        try:
            bounded_text(text, 512)
        except ValueError:
            raise SafetyStop("invalid-family-notice-text") from None
    return frozenset(rooms), frozenset(users), cleaned


def saved_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    """The routing facts a saved inbox is bound to; editing them is a SafetyStop."""
    family_rooms, family_users, family_devices = family_config(config)
    policy: dict[str, Any] = {key: config[key] for key in POLICY_KEYS}
    policy["family_rooms"] = sorted(family_rooms)
    policy["family_users"] = sorted(family_users)
    policy["family_devices"] = family_devices
    policy["identities"] = identities(config)
    return policy


def upgrade_saved_policy(old: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a policy saved by an earlier release before comparison.

    Stage-1 predecessors had no family settings (empty is the safe upgrade)
    and the pilot recorded its worker command / remote flag, which no longer
    exist. Neither difference reroutes saved jobs, so both upgrade silently.
    """
    upgraded = {key: value for key, value in old.items() if key not in LEGACY_POLICY_KEYS}
    upgraded.setdefault("family_rooms", [])
    upgraded.setdefault("family_users", [])
    upgraded.setdefault("family_devices", {})
    upgraded.setdefault("identities", {})
    return upgraded


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def turn_id(event_id: str) -> str:
    return hashlib.sha256(event_id.encode()).hexdigest()[:32]


def parts(text: str, limit: int = 12_000) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        n = len(char.encode())
        if size + n > limit:
            result.append("".join(current))
            current = []
            size = 0
        current.append(char)
        size += n
    if current:
        result.append("".join(current))
    return result


def operator_name(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    try:
        name = environ.get("SUDO_USER") or getpass.getuser()
    except (KeyError, OSError):
        name = "uid" + str(os.getuid())
    return name + "#" + str(os.getuid())


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs (
        seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
        room_id TEXT NOT NULL, sender TEXT NOT NULL, scope TEXT NOT NULL,
        body TEXT NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','uncertain','ready','done')),
        reply TEXT, txn_id TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS sessions (scope TEXT PRIMARY KEY, session_id TEXT NOT NULL);
"""

_MATRIX_SCHEMA = """
    CREATE TABLE IF NOT EXISTS deliveries(event_id TEXT PRIMARY KEY, part INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS controls(event_id TEXT PRIMARY KEY, digest TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS operator_audit(seq INTEGER PRIMARY KEY, at REAL NOT NULL,
        actor TEXT NOT NULL, action TEXT NOT NULL, scope TEXT NOT NULL,
        reason TEXT NOT NULL, before TEXT NOT NULL);
"""

_STATE_FILES = ("inbox.sqlite3", "inbox.sqlite3-journal", "inbox.sqlite3-wal", "inbox.sqlite3-shm")


class Store:
    """Single-process inbox/outbox; uncertain execution is never automatically replayed."""

    def __init__(self, directory: Path | str, account: str, *, total_cap: int = 128, scope_cap: int = 32) -> None:
        self._db: sqlite3.Connection | None = None
        self.directory_fd: int | None = None
        self.lock_fd: int | None = None
        if (
            not identifier(account, "@")
            or type(total_cap) is not int
            or type(scope_cap) is not int
            or not 1 <= scope_cap <= total_cap <= 1000
        ):
            raise ValueError("invalid store settings")
        self.account = account
        self.total_cap = total_cap
        self.scope_cap = scope_cap
        try:
            self.directory_fd = private_directory(directory)
            self.lock_fd = private_file(self.directory_fd, "inbox.lock")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # SQLite may recover a rollback journal. Reject unsafe preexisting
            # journal/sidecar files before SQLite sees any of their paths.
            for name in _STATE_FILES:
                try:
                    os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                os.close(private_file(self.directory_fd, name))
            os.close(private_file(self.directory_fd, "inbox.sqlite3"))
            self._db = sqlite3.connect(f"/proc/self/fd/{self.directory_fd}/inbox.sqlite3")
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            with self._db:
                for key, value in (("account", account), ("schema", "1")):
                    old = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                    if old and old[0] != value:
                        raise ValueError("state identity/schema mismatch")
                    self._db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
                self._db.execute("UPDATE jobs SET state='uncertain' WHERE state='running'")
        except BaseException:
            self.close()
            raise

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise ValueError("store is closed")
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        for name in ("lock_fd", "directory_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def token(self) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key='sync_token'").fetchone()
        return row[0] if row else None

    def accept_batch(self, requests: Iterable[Request], next_token: str | None) -> None:
        """Commit admitted input and the /sync token together, or neither."""
        if next_token is not None:
            bounded_text(next_token, 4096)
        with self.db:
            for req in requests:
                if (
                    not isinstance(req, Request)
                    or not identifier(req.event_id, "$")
                    or not identifier(req.room_id, "!")
                    or not identifier(req.sender, "@")
                ):
                    raise ValueError("invalid request")
                bounded_text(req.body, MAX_TEXT_BYTES)
                if req.scope != scope_of(self.account, req.room_id, req.sender):
                    raise ValueError("request belongs to another scope/account")
                digest = hashlib.sha256(json.dumps([req.room_id, req.sender, req.body]).encode()).hexdigest()
                old = self.db.execute("SELECT digest FROM jobs WHERE event_id=?", (req.event_id,)).fetchone()
                if old:
                    if old[0] != digest:
                        raise ValueError("event identity conflict")
                    continue
                count, scoped = self.db.execute(
                    "SELECT count(*), coalesce(sum(scope=?),0) FROM jobs WHERE state!='done'",
                    (req.scope,),
                ).fetchone()
                if count >= self.total_cap or scoped >= self.scope_cap:
                    raise QueueFull("inbox capacity reached; sync token unchanged")
                txn = hashlib.sha256(json.dumps([self.account, req.event_id, "reply-v1"]).encode()).hexdigest()
                self.db.execute(
                    "INSERT INTO jobs(event_id,room_id,sender,scope,body,digest,state,txn_id) "
                    "VALUES (?,?,?,?,?,?,'queued',?)",
                    (req.event_id, req.room_id, req.sender, req.scope, req.body, digest, txn),
                )
            if next_token is not None:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('sync_token',?)", (next_token,))

    def job_exists(self, event_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM jobs WHERE event_id=?", (event_id,)).fetchone() is not None

    def pending_before(self, event_id: str) -> int:
        """Real (non-notice) undelivered jobs of the same scope queued earlier."""
        row = self.db.execute(
            "SELECT COUNT(*) FROM jobs q WHERE q.scope=(SELECT scope FROM jobs WHERE event_id=?) "
            "AND q.state!='done' AND q.seq<(SELECT seq FROM jobs WHERE event_id=?) "
            "AND q.event_id NOT LIKE '$notice-%'",
            (event_id, event_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def claim(self) -> dict[str, Any] | None:
        with self.db:
            row = self.db.execute(
                "SELECT * FROM jobs q WHERE state='queued' AND NOT EXISTS "
                "(SELECT 1 FROM jobs p WHERE p.scope=q.scope AND p.seq<q.seq AND p.state!='done') "
                "ORDER BY seq LIMIT 1"
            ).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='running' WHERE event_id=?", (row["event_id"],))
        return dict(row) if row else None

    def finish(self, event_id: str, reply: str, session_id: str | None = None) -> None:
        """Record a confirmed result. An empty reply completes the job without delivery."""
        if not isinstance(reply, str):
            raise ValueError("invalid text")
        deliver = bool(reply.strip())
        if deliver:
            bounded_text(reply, MAX_REPLY_BYTES)
        if session_id is not None:
            bounded_text(session_id, 255)
        with self.db:
            row = self.db.execute("SELECT scope,state FROM jobs WHERE event_id=?", (event_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise ValueError("only running work can finish")
            self.db.execute(
                "UPDATE jobs SET state=?,reply=? WHERE event_id=?",
                ("ready" if deliver else "done", reply, event_id),
            )
            if session_id is not None:
                self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (row["scope"], session_id))

    def session(self, scope: str) -> str | None:
        row = self.db.execute("SELECT session_id FROM sessions WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else None

    def outbox(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs WHERE state='ready' ORDER BY seq")]

    def delivered(self, event_id: str) -> None:
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET state='done' WHERE event_id=? AND state='ready'", (event_id,)
            ).rowcount
            if changed != 1:
                raise ValueError("only a pending reply can be acknowledged")

    def uncertain(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs WHERE state='uncertain' ORDER BY seq")]

    def resolve_uncertain(self, event_id: str, reply: str) -> None:
        """Operator reconciliation result; never rerun an uncertain operation."""
        bounded_text(reply, MAX_REPLY_BYTES)
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET state='ready',reply=? WHERE event_id=? AND state='uncertain'",
                (reply, event_id),
            ).rowcount
            if changed != 1:
                raise ValueError("no uncertain work to resolve")


class MatrixStore(Store):
    """Store plus sync replay, chunked-delivery progress, control dedup and operator audit."""

    def __init__(self, directory: Path | str, account: str) -> None:
        super().__init__(directory, account)
        try:
            self.db.executescript(_MATRIX_SCHEMA)
        except BaseException:
            self.close()
            raise

    # -- operator block -------------------------------------------------------

    def block(self) -> dict[str, Any] | None:
        """Describe uncertain work that pauses the transport, without message bodies, or None."""
        jobs = self.uncertain()
        if not jobs:
            return None
        scopes = sorted({row["scope"] for row in jobs})
        turns = [{"turn_id": turn_id(row["event_id"]), "scope": row["scope"], "room_id": row["room_id"]} for row in jobs]
        return {"blocked_scopes": scopes, "uncertain_scopes": scopes, "uncertain_turns": turns}

    def unblock(self, scope: str, reason: str, actor: str) -> dict[str, Any]:
        """Operator decision: acknowledge every uncertain job of one scope and audit who/when/why.

        Like ``/ack``, this records the reconciliation as a reply and never
        re-runs the work.
        """
        if not isinstance(scope, str) or not SCOPE_PATTERN.fullmatch(scope):
            raise ValueError("invalid-scope")
        bounded_text(reason, 1024)
        bounded_text(actor, 255)
        before = self.block()
        if before is None:
            raise ValueError("no-block")
        if scope not in before["blocked_scopes"]:
            raise ValueError("scope-not-blocked")
        at = time.time()
        cleared = [row["event_id"] for row in self.uncertain() if row["scope"] == scope]
        with self.db:
            for event_id in cleared:
                self.db.execute(
                    "UPDATE jobs SET state='ready',reply=? WHERE event_id=? AND state='uncertain'",
                    (OPERATOR_ACK_TEXT, event_id),
                )
            seq = self.db.execute(
                "INSERT INTO operator_audit(at,actor,action,scope,reason,before) VALUES (?,?,?,?,?,?)",
                (at, actor, "unblock", scope, reason, json.dumps(before)),
            ).lastrowid
        return {
            "cleared": cleared,
            "scope": scope,
            "actor": actor,
            "at": at,
            "audit_seq": seq,
            "before": before,
        }

    def audit(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM operator_audit ORDER BY seq")]

    # -- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def storage_gate(self) -> None:
        assert self.directory_fd is not None
        fs = os.fstatvfs(self.directory_fd)
        size = os.stat("inbox.sqlite3", dir_fd=self.directory_fd, follow_symlinks=False).st_size
        if fs.f_bavail * fs.f_frsize < 268_435_456 or size > 134_217_728:
            raise SafetyStop("pilot-storage-limit")

    # -- sync replay ----------------------------------------------------------

    def stage_sync(self, raw: Any) -> None:
        if self.get_meta("pending_sync") is not None:
            raise SafetyStop("pending-sync-not-finished")
        if not isinstance(raw, dict):
            raise SafetyStop("invalid-sync")
        bounded_text(raw.get("next_batch"), 4096)
        if len(json.dumps(raw).encode()) > 4_194_304:
            raise SafetyStop("sync-too-large")
        self.set_meta("pending_sync", raw)

    def commit_sync(self, token: str) -> None:
        pending = self.get_meta("pending_sync")
        if not pending or pending.get("next_batch") != token:
            raise SafetyStop("sync-identity-mismatch")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('sync_token',?)", (token,))
            self.db.execute("UPDATE meta SET value='null' WHERE key='pending_sync'")

    # -- controls, notices, deliveries ---------------------------------------

    def seen_control(self, req: Request, *, record: bool = True) -> bool:
        digest = hashlib.sha256(json.dumps([req.room_id, req.sender, req.body]).encode()).hexdigest()
        with self.db:
            row = self.db.execute("SELECT digest FROM controls WHERE event_id=?", (req.event_id,)).fetchone()
            if row:
                if row[0] != digest:
                    raise SafetyStop("control-identity-conflict")
                return True
            if record:
                self.db.execute("INSERT INTO controls VALUES (?,?)", (req.event_id, digest))
        return False

    def notice(self, req: Request, key: str, text: str) -> str:
        """Queue a durable, idempotent notice for ``req``'s room; returns its outbox event id."""
        bounded_text(text, MAX_REPLY_BYTES)
        event = "$notice-" + hashlib.sha256(json.dumps([req.event_id, key]).encode()).hexdigest()
        txn = hashlib.sha256(json.dumps([self.account, event, "reply-v1"]).encode()).hexdigest()
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.db:
            old = self.db.execute("SELECT digest FROM jobs WHERE event_id=?", (event,)).fetchone()
            if old:
                if old[0] != digest:
                    raise SafetyStop("notice-identity-conflict")
                return event
            self.db.execute(
                "INSERT INTO jobs(event_id,room_id,sender,scope,body,digest,state,reply,txn_id) "
                "VALUES (?,?,?,?,?,?,'ready',?,?)",
                (event, req.room_id, req.sender, req.scope, "notice", digest, text, txn),
            )
        return event

    def delivered_parts(self, event: str) -> int:
        row = self.db.execute("SELECT part FROM deliveries WHERE event_id=?", (event,)).fetchone()
        return int(row[0]) if row else 0

    def mark_part(self, event: str, part: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?)", (event, part))

    def uncertain_job(self, event: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET state='uncertain' WHERE event_id=? AND state='running'", (event,))
