"""Bridge ``.env`` fallback for os.environ-only operational switches (#2841).

``Config.load`` merges the process environment, the project ``.env`` and the
package fallback ``.env`` into the settings object without exporting anything.
A family of operational knobs, however, is read straight from ``os.environ``
by ``ExternalWaitMonitor.env_int`` / ``env_flag`` (the external-wait resume
cap, the continuation cap, turn-age thresholds, ...). ``start.sh`` used to
paper over that by exporting the ``.env`` files, but the Hande and Matrix
entry points start ``python -m telegram_bot`` directly (systemd unit, or a
wrapper that clears the environment), so a value written only to ``.env`` was
silently ignored and the built-in default won.

The fix keeps ``load`` export-free: the bridge entry point installs the
``.env`` layers here once, and the two helpers consult them only when the
process environment does not carry the key. Precedence therefore matches
every other setting — process > project ``.env`` > package fallback ``.env``.

Only ``CCC_``-prefixed names are kept, and names that look like credentials
are dropped: the helpers parse integers and booleans, so nothing secret ever
needs to live here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final, Optional

PREFIX: Final = "CCC_"
_CREDENTIAL_FRAGMENTS: Final = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "KEY", "CREDENTIAL")

_values: dict[str, str] = {}


def select_operational_values(layers: Mapping[str, Optional[str]]) -> dict[str, str]:
    """Return the ``CCC_*`` non-credential, non-empty entries of ``layers``."""

    selected: dict[str, str] = {}
    for name, value in layers.items():
        if not isinstance(name, str) or not name.startswith(PREFIX):
            continue
        upper = name.upper()
        if any(fragment in upper for fragment in _CREDENTIAL_FRAGMENTS):
            continue
        if not isinstance(value, str) or "\x00" in value or not value.strip():
            continue
        selected[name] = value
    return selected


def install(layers: Mapping[str, Optional[str]]) -> None:
    """Replace the installed fallback with the operational subset of ``layers``."""

    global _values
    _values = select_operational_values(layers)


def install_from_settings(settings: Any) -> None:
    """Install the layers captured by ``Config.load`` (empty when absent).

    Settings objects without the capture (test stubs, older callers)
    contribute nothing instead of failing startup.
    """

    getter = getattr(settings, "dotenv_operational_environment", None)
    values = getter() if callable(getter) else None
    install(values if isinstance(values, Mapping) else {})


def clear() -> None:
    """Drop the installed fallback (tests, re-exec)."""

    global _values
    _values = {}


def installed() -> dict[str, str]:
    """Return a copy of the installed fallback."""

    return dict(_values)


def lookup(name: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the process value of ``name`` or, when absent, the ``.env`` value.

    A key present in the process environment always wins, even when blank —
    the same rule ``Config.load`` applies when it merges the layers.
    """

    env = os.environ if environ is None else environ
    if name in env:
        return env[name]
    return _values.get(name)


def merged_environ(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Return ``environ`` (default: the process env) over the installed fallback.

    For readers that take a whole mapping rather than one name (the webhook
    nudge builders). A key present in ``environ`` wins even when blank, as in
    :func:`lookup`; credential-looking names are never in the fallback, so a
    secret such as ``CCC_WEBHOOK_NUDGE_SECRET`` still comes from the process
    environment only.
    """

    env = os.environ if environ is None else environ
    merged = dict(_values)
    merged.update(env)
    return merged
