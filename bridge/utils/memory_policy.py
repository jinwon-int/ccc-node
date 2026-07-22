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
    "set CCC_CODEX_AUDIENCE_AUTH_MODE=keyring after provisioning Codex "
    "credentials in the OS keyring. File credentials are not copied into "
    "audience homes."
)


def assert_memory_provider_safe(
    mode: str,
    provider: str,
    codex_audience_auth_mode: str = "disabled",
) -> None:
    """Raise ValueError for provider/memory-mode combinations that leak.

    Codex is allowed only with its officially supported OS keyring credential
    store. A file-backed login lives inside ``CODEX_HOME`` and copying it into
    each audience would multiply long-lived access tokens.
    """

    if (
        mode == MEMORY_MODE_AUDIENCE_SCOPED
        and str(provider or "").strip().lower() == "codex"
        and str(codex_audience_auth_mode or "").strip().lower() != "keyring"
    ):
        raise ValueError(_CODEX_AUDIENCE_ERROR)
