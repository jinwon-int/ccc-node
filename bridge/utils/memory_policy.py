"""Shared bridge-memory policy invariants.

Single home of the "shared-all session scope + bridge memory is unsafe"
rule that was previously implemented three times (the config model
validator, ``curated_memory`` and ``memory_audience``) and could drift
(#584 P0-4).
"""

from __future__ import annotations

MEMORY_MODE_OFF = "off"
MEMORY_MODE_CURATED = "curated"
MEMORY_MODE_AUDIENCE_SCOPED = "audience-scoped"

_SHARED_ALL_ERROR = (
    "bridge memory cannot run with CCC_TELEGRAM_SESSION_SCOPE=shared-all: "
    "shared-all is unsafe. Use shared-groups, or set "
    "CCC_BRIDGE_UNSAFE_SHARED_ALL_MEMORY=true only for intentional legacy "
    "curated behavior."
)


def assert_memory_scope_safe(
    mode: str, session_scope: object, *, unsafe_shared_all_override: bool = False
) -> None:
    """Raise ``ValueError`` when bridge memory would run under shared-all.

    Audience-scoped memory never accepts the unsafe override; curated mode
    does, for intentional legacy setups only. ``session_scope`` is normalized
    the same way ``session_scope.normalize_session_scope`` does before the
    shared-all comparison, so alias spellings cannot bypass the gate.
    """

    scope = str(session_scope or "").strip().lower().replace("_", "-")
    if scope != "shared-all" or mode == MEMORY_MODE_OFF:
        return
    if mode == MEMORY_MODE_CURATED and unsafe_shared_all_override:
        return
    raise ValueError(_SHARED_ALL_ERROR)
