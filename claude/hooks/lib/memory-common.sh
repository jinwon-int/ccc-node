#!/usr/bin/env bash
# memory-common.sh — shared audience-scope environment and validation for the
# memory hooks (load-memory.sh, refresh-memory.sh).
#
# Source this AFTER the consumer has set its own STATE_DIR/CACHE defaults and
# AFTER lib/hook-common.sh (for is_disabled). It defines the audience env vars
# and the security-critical core validator that both hooks previously
# implemented separately (epic #584 P1-2) — a divergence there is a fail-open
# cross-audience leak hazard, so the core rules live in exactly one place.

AUDIENCE_SCOPED="${CCC_MEMORY_AUDIENCE_SCOPED:-0}"
MEMORY_AUDIENCE="${CCC_MEMORY_AUDIENCE:-legacy}"
MEMORY_SCOPE="${CCC_MEMORY_SCOPE:-}"
AUDIENCE_ROOT="${CCC_MEMORY_AUDIENCE_ROOT:-}"
SHARED_STATE_DIR="${CCC_MEMORY_SHARED_STATE_DIR:-}"
SHARED_CACHE_DIR="${CCC_MEMORY_SHARED_CACHE_DIR:-}"
SHARED_MEMDIR="${CCC_MEMORY_SHARED_DIR:-}"
SHARED_FACTS_FILE="${CCC_MEMORY_SHARED_FACTS_FILE:-}"
HONCHO_AUDIENCE_SCOPED="${CCC_HONCHO_AUDIENCE_SCOPED:-0}"
HONCHO_WORKSPACE_SCOPE="${CCC_HONCHO_WORKSPACE_SCOPE:-}"
HONCHO_SHARED_WORKSPACE_SCOPE="${CCC_HONCHO_SHARED_WORKSPACE_SCOPE:-}"

# memory_scope_core_valid — audience:scope shape plus the scoped paths every
# memory hook shares. Callers add their hook-specific path checks on top:
#   memory_scope_core_valid && [ "$EXTRA" = "$AUDIENCE_ROOT/..." ] || fail
memory_scope_core_valid() {
  local suffix
  [ -n "$AUDIENCE_ROOT" ] || return 1
  case "$MEMORY_AUDIENCE:$MEMORY_SCOPE" in
    shared:shared) ;;
    private:private-*)
      suffix="${MEMORY_SCOPE#private-}"
      [ "${#suffix}" = 32 ] || return 1
      case "$suffix" in *[!0-9a-f]*) return 1 ;; esac
      ;;
    *) return 1 ;;
  esac
  [ "$STATE_DIR" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/state" ] \
    && [ "$CACHE" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/cache" ] \
    && [ "$SHARED_STATE_DIR" = "$AUDIENCE_ROOT/shared/state" ] \
    && [ "$SHARED_CACHE_DIR" = "$AUDIENCE_ROOT/shared/cache" ] \
    && [ "$SHARED_MEMDIR" = "$AUDIENCE_ROOT/shared/memories" ]
}

# Honcho may be enabled in audience mode only when its server-side workspace
# suffixes are bound to the same validated opaque route.
honcho_scope_valid() {
  ! is_disabled "$HONCHO_AUDIENCE_SCOPED" \
    && [ "$HONCHO_WORKSPACE_SCOPE" = "$MEMORY_SCOPE" ] \
    && [ "$HONCHO_SHARED_WORKSPACE_SCOPE" = "shared" ]
}
