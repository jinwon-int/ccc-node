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
    each audience would multiply long-lived access tokens. Piri keeps its
    operator-owned provider configuration global while the runtime separately
    isolates transcripts and generated memory context per audience.
    """

    if (
        mode == MEMORY_MODE_AUDIENCE_SCOPED
        and str(provider or "").strip().lower() == "codex"
        and str(codex_audience_auth_mode or "").strip().lower() != "keyring"
    ):
        raise ValueError(_CODEX_AUDIENCE_ERROR)


def owner_global_memory_gate(settings: object) -> tuple[bool, str, str]:
    """Is this bridge the owner's own, so the node-global memory is theirs?

    The single privacy gate for the node-global (unpartitioned) memory lane,
    shared by the write side (``__main__._build_global_local_sink_worker``,
    #2296) and the read side (the curated ``nunchi/sessionstart.sh`` hook,
    #2844) so the two cannot drift. True only for an ``off``/``curated``
    bridge whose resolved execution profile is ``owner-operator`` (exactly
    one allowlisted user) and whose session scope is not ``shared-all``.

    Returns ``(allowed, profile, scope)``; the last two are for log lines.
    Imports are local: ``core`` modules import this module.
    """

    from telegram_bot.core import tool_policy
    from telegram_bot.core.session_scope import (
        SESSION_SCOPE_SHARED_ALL,
        normalize_session_scope,
    )

    profile = tool_policy.resolve_execution_profile(
        getattr(settings, "execution_profile", None),
        allowed_user_ids=getattr(settings, "allowed_user_ids", None),
        require_allowlist=getattr(settings, "require_allowlist", True),
    )
    scope = normalize_session_scope(getattr(settings, "telegram_session_scope", None))
    mode = getattr(settings, "bridge_memory_mode", MEMORY_MODE_OFF)
    allowed = (
        mode in {MEMORY_MODE_OFF, MEMORY_MODE_CURATED}
        and profile == tool_policy.EXECUTION_OWNER_OPERATOR
        and scope != SESSION_SCOPE_SHARED_ALL
    )
    return allowed, profile, scope
