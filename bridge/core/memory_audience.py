"""Fail-closed audience boundaries for Telegram bridge memory hooks.

The bridge deliberately keeps provider conversation routing separate from
memory routing.  Public Telegram surfaces share one bounded ``shared`` memory
audience, while every DM gets an opaque private audience.  Raw Telegram ids are
never written into audience paths, hook settings, or memory metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_bot.core.session_scope import is_group_conversation
from telegram_bot.utils.memory_policy import (
    MEMORY_MODE_AUDIENCE_SCOPED,
    assert_memory_scope_safe,
)


AUDIENCE_PRIVATE = "private"
AUDIENCE_SHARED = "shared"
_KEY_BYTES = 32


@dataclass(frozen=True)
class MemoryAudience:
    """One route's non-secret, non-reversible memory storage coordinates."""

    kind: str
    scope: str
    root: Path

    @property
    def scope_root(self) -> Path:
        return self.root / self.scope

    @property
    def state_dir(self) -> Path:
        return self.scope_root / "state"

    @property
    def cache_dir(self) -> Path:
        return self.scope_root / "cache"

    @property
    def memory_dir(self) -> Path:
        return self.scope_root / "memories"

    @property
    def shared_root(self) -> Path:
        return self.root / AUDIENCE_SHARED

    @property
    def codex_home(self) -> Path:
        """Return the provider store dedicated to this opaque audience."""

        return self.scope_root / "codex"

    def hook_environment(self, settings: Any) -> dict[str, str]:
        """Return body-free paths/policy for the existing memory hook stack."""

        claude_root = Path(settings.claude_settings_path).expanduser().parent
        env = {
            "CCC_MEMORY_AUDIENCE_SCOPED": "1",
            "CCC_MEMORY_AUDIENCE": self.kind,
            "CCC_MEMORY_SCOPE": self.scope,
            "CCC_MEMORY_AUDIENCE_ROOT": str(self.root),
            "CCC_STATE_DIR": str(self.state_dir),
            "CCC_MEMORY_CACHE_DIR": str(self.cache_dir),
            "CCC_MEMORY_DIR": str(self.memory_dir),
            "CCC_MEMORY_INDEX_DB": str(self.state_dir / "memory-index.sqlite"),
            "CCC_MEMORY_FACTS_FILE": str(self.state_dir / "memory-facts.jsonl"),
            "CCC_RESUME_FILE": str(self.state_dir / "resume.md"),
            "CCC_MEMORY_SHARED_STATE_DIR": str(self.shared_root / "state"),
            "CCC_MEMORY_SHARED_CACHE_DIR": str(self.shared_root / "cache"),
            "CCC_MEMORY_SHARED_DIR": str(self.shared_root / "memories"),
            "CCC_MEMORY_SHARED_FACTS_FILE": str(
                self.shared_root / "state" / "memory-facts.jsonl"
            ),
            # Unscoped data predates audience labels.  It is private legacy
            # input and load-memory.sh reads it only for private DM audiences.
            "CCC_MEMORY_LEGACY_STATE_DIR": str(claude_root / "state"),
            "CCC_MEMORY_LEGACY_CACHE_DIR": str(claude_root / "hooks" / "cache"),
            "CCC_MEMORY_LEGACY_DIR": str(claude_root / "memories"),
            "CCC_MEMORY_LEGACY_RESUME_FILE": str(claude_root / "state" / "resume.md"),
            # Honcho derives a distinct server-side workspace from this opaque
            # scope. Private recall may additionally read the shared workspace
            # and the private-only legacy workspace; public routes never do.
            "CCC_HONCHO_MEMORY_ENABLED": (
                "1" if getattr(settings, "honcho_memory_enabled", False) else "0"
            ),
            "CCC_HONCHO_AUDIENCE_SCOPED": "1",
            "CCC_HONCHO_WORKSPACE_SCOPE": self.scope,
            "CCC_HONCHO_SHARED_WORKSPACE_SCOPE": AUDIENCE_SHARED,
            "CCC_HONCHO_CFG": str(
                Path(
                    getattr(
                        settings,
                        "honcho_config_path",
                        Path.home() / ".hermes" / "honcho.json",
                    )
                ).expanduser()
            ),
            # Family Wiki reads still use one global cache. Candidate writes
            # are separately routed by the bridge worker, but hook reads stay
            # disabled until they have an audience filter.
            "CCC_WIKI_MEMORY_ENABLED": "0",
        }
        return env

    def codex_environment(self, settings: Any) -> dict[str, str]:
        """Return the audience overlay for one future Codex process.

        Both the main Codex home and its optional SQLite override are scoped so
        no process-level persistence can silently fall back to a global store.
        The caller remains responsible for merging this body-free overlay with
        its normal process environment.
        """

        env = self.hook_environment(settings)
        env.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "CODEX_SQLITE_HOME": str(self.codex_home),
                "CCC_CODEX_AUDIENCE_AUTH_MODE": str(
                    getattr(settings, "codex_audience_auth_mode", "disabled")
                ),
            }
        )
        return env

    def claude_environment(self, settings: Any) -> dict[str, str]:
        """Return the exact hook overlay for one Claude SDK session.

        Claude receives this mapping through ``SessionRequest`` and rebuilds
        it from the resolved route before constructing flag settings. Keeping
        the provider method separate from ``codex_environment`` prevents
        Codex credential/home variables from leaking into Claude settings.
        """

        return self.hook_environment(settings)


def _audience_root(settings: Any) -> Path:
    configured = getattr(settings, "bridge_memory_audience_root", None)
    if configured:
        return Path(configured).expanduser().resolve()
    data_dir = getattr(settings, "bot_data_dir", None)
    if data_dir:
        return Path(data_dir).expanduser().resolve() / "memory-audiences"
    project_root = Path(getattr(settings, "project_root")).expanduser().resolve()
    return project_root / ".telegram_bot" / "memory-audiences"


def _key_path(settings: Any) -> Path:
    configured = getattr(settings, "bridge_memory_audience_key_path", None)
    if configured:
        return Path(configured).expanduser().resolve()
    return _audience_root(settings).parent / "memory-audience.key"


def audience_from_claude_environment(
    settings: Any, environment: Mapping[str, str] | None
) -> MemoryAudience:
    """Reconstruct and validate one Claude route without trusting its paths.

    ``SessionRequest.memory_environment`` is provider-neutral and therefore
    intentionally opaque to the runtime contract.  The Claude adapter must not
    inject an arbitrary caller-supplied environment into hook settings, so this
    helper accepts only the byte-for-byte mapping that the canonical audience
    resolver would have produced for the declared kind/scope.
    """

    if environment is None:
        raise ValueError("Claude audience-scoped memory requires a route environment")
    kind = environment.get("CCC_MEMORY_AUDIENCE")
    scope = environment.get("CCC_MEMORY_SCOPE")
    if kind == AUDIENCE_SHARED:
        if scope != AUDIENCE_SHARED:
            raise ValueError("Claude shared memory route is invalid")
    elif kind == AUDIENCE_PRIVATE:
        suffix = (scope or "").removeprefix("private-")
        if (
            not isinstance(scope, str)
            or not scope.startswith("private-")
            or len(suffix) != 32
            or any(char not in "0123456789abcdef" for char in suffix)
        ):
            raise ValueError("Claude private memory route is invalid")
    else:
        raise ValueError("Claude memory audience is invalid")

    assert isinstance(kind, str) and isinstance(scope, str)
    audience = MemoryAudience(kind, scope, _audience_root(settings))
    expected = audience.claude_environment(settings)
    if dict(environment) != expected:
        raise ValueError("Claude audience environment does not match the resolved route")
    return audience


def _read_private_key(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"memory audience key is not a regular file: {path}")
        if info.st_uid != os.geteuid():
            raise ValueError(
                f"memory audience key must be owned by the bridge user: {path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(f"memory audience key permissions must be 0600: {path}")
        value = os.read(fd, 4096)
    finally:
        os.close(fd)
    if len(value) != _KEY_BYTES:
        raise ValueError(f"memory audience key must contain {_KEY_BYTES} bytes: {path}")
    return value


def load_or_create_audience_key(settings: Any) -> bytes:
    """Return a stable local HMAC key, creating it atomically with mode 0600."""

    path = _key_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_info = path.parent.stat()
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError(f"memory audience key parent is not a directory: {path.parent}")
    # Path.mkdir(mode=..., exist_ok=True) only applies the mode when it *creates*
    # the directory; a .telegram_bot created earlier by start.sh/legacy code under
    # the default umask 022 (-> 0755) is left untouched and would then fail the
    # guard below forever. Self-heal a bridge-OWNED parent by tightening it to
    # 0700. A parent owned by another user is a real exposure and still raises.
    if parent_info.st_uid == os.geteuid() and stat.S_IMODE(parent_info.st_mode) & 0o077:
        os.chmod(path.parent, 0o700)
        parent_info = path.parent.stat()
    if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
        raise ValueError(
            "memory audience key parent must be bridge-owned and mode 0700: "
            f"{path.parent}"
        )
    try:
        return _read_private_key(path)
    except FileNotFoundError:
        value = secrets.token_bytes(_KEY_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return _read_private_key(path)
        try:
            os.write(fd, value)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
        return value


def resolve_memory_audience(
    settings: Any, *, user_id: int, chat_id: int | None
) -> MemoryAudience | None:
    """Resolve one route, returning ``None`` unless the safe mode is enabled."""

    if getattr(settings, "bridge_memory_mode", "off") != MEMORY_MODE_AUDIENCE_SCOPED:
        return None
    if chat_id is None:
        raise ValueError("audience-scoped memory requires a Telegram chat id")
    assert_memory_scope_safe(
        MEMORY_MODE_AUDIENCE_SCOPED,
        getattr(settings, "telegram_session_scope", "per-user-chat"),
    )

    root = _audience_root(settings)
    if is_group_conversation(user_id, chat_id):
        return MemoryAudience(AUDIENCE_SHARED, AUDIENCE_SHARED, root)

    key = load_or_create_audience_key(settings)
    digest = hmac.new(
        key,
        f"telegram-dm-user\0{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return MemoryAudience(AUDIENCE_PRIVATE, f"private-{digest}", root)


def shared_memory_audience(settings: Any) -> MemoryAudience:
    """Return the route-neutral shared audience for safe metadata operations."""

    if getattr(settings, "bridge_memory_mode", "off") != MEMORY_MODE_AUDIENCE_SCOPED:
        raise ValueError("shared memory audience requires audience-scoped mode")
    assert_memory_scope_safe(
        MEMORY_MODE_AUDIENCE_SCOPED,
        getattr(settings, "telegram_session_scope", "per-user-chat"),
    )
    return MemoryAudience(AUDIENCE_SHARED, AUDIENCE_SHARED, _audience_root(settings))
