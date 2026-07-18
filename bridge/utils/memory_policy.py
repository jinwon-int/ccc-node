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


_CODEX_AUDIENCE_ERROR = (
    "audience-scoped memory cannot run with the Codex provider (#581): "
    "CODEX_HOME/AGENTS.md is a single global persistent store with no "
    "per-audience separation, so DM-private memory could reach "
    "shared-audience Codex runs. Use the Claude provider, or keep "
    "CCC_BRIDGE_MEMORY_MODE off/curated for Codex nodes."
)


def assert_memory_provider_safe(mode: str, provider: str) -> None:
    """Raise ValueError for provider/memory-mode combinations that leak.

    The Codex launch surface has no audience-scoped memory store yet
    (jinwon-int/ccc-node#581); refusing the configuration at load time keeps
    the #580 isolation guarantee intact until CODEX_HOME is scoped.
    """

    if mode == MEMORY_MODE_AUDIENCE_SCOPED and str(provider or "").strip().lower() == "codex":
        raise ValueError(_CODEX_AUDIENCE_ERROR)
