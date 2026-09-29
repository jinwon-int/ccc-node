"""Explicit hand-off of wrapper-only settings to provider child processes (#1771).

``Config.load`` merges the process environment, the project ``.env`` and the
package fallback ``.env`` without mutating ``os.environ``. The ccc-piri /
ccc-codex launcher wrappers, however, read a handful of settings ONLY from
their own process environment (``CCC_PIRI_REAL_CLI_PATH`` and friends). A key
written to the project ``.env`` — or to ``bridge/.env`` for a unit that does
not start through ``start.sh``, such as the Matrix frontend — therefore reached
the settings object but never the wrapper child, which fell back to a bare
``piri`` / ``codex`` PATH lookup and exited 127.

The fix is deliberately narrow: only the keys in ``WRAPPER_ENV_KEYS`` are
captured from the merged configuration, and they are added to a child
environment only where that environment does not already carry the key. The
whole configuration is never exported — it holds provider secrets.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# Keys the launcher wrappers (scripts/ccc-piri, scripts/ccc-codex) read from
# their own environment and nothing else. Paths and a boolean switch only; no
# secret may ever be added here.
WRAPPER_ENV_KEYS: Final = (
    "CCC_PIRI_REAL_CLI_PATH",
    "CCC_PIRI_MEMORY_MATERIALIZER_PATH",
    "CCC_PIRI_MEMORY_HOME",
    "CCC_PIRI_MEMORY_SKIP",
    "CCC_CODEX_REAL_CLI_PATH",
    "CCC_CODEX_MEMORY_MATERIALIZER_PATH",
)


def select_wrapper_environment(merged: Mapping[str, str | None]) -> dict[str, str]:
    """Return the allowlisted, non-empty wrapper keys from a merged mapping."""

    selected: dict[str, str] = {}
    for name in WRAPPER_ENV_KEYS:
        value = merged.get(name)
        if not isinstance(value, str) or "\x00" in value or not value.strip():
            continue
        selected[name] = value
    return selected


def wrapper_environment(settings: Any) -> dict[str, str]:
    """Return the wrapper keys captured by ``Config.load`` (empty otherwise)."""

    getter = getattr(settings, "wrapper_environment", None)
    values = getter() if callable(getter) else None
    if not isinstance(values, Mapping):
        return {}
    return select_wrapper_environment(values)


def missing_wrapper_environment(
    settings: Any, base: Mapping[str, str]
) -> dict[str, str]:
    """Return only the captured wrapper keys that ``base`` does not carry."""

    return {
        name: value
        for name, value in wrapper_environment(settings).items()
        if name not in base
    }


def with_wrapper_environment(base: Mapping[str, str], settings: Any) -> dict[str, str]:
    """Copy ``base`` and add the captured wrapper keys it does not carry.

    An existing key always wins: the process environment already outranks the
    ``.env`` files in ``Config.load``, and an audience route overlay must never
    be overwritten by a node-global value.
    """

    environment = dict(base)
    environment.update(missing_wrapper_environment(settings, base))
    return environment


__all__ = [
    "WRAPPER_ENV_KEYS",
    "missing_wrapper_environment",
    "select_wrapper_environment",
    "with_wrapper_environment",
    "wrapper_environment",
]
