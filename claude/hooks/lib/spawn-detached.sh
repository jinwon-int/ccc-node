#!/usr/bin/env bash
# Shared detached-process launcher for Claude hooks.
#
# spawn_detached <script> <reentry-env-name-or-empty> <fallback-function> [args...]
#
# Prefer a new session so ssh/CLI process-group teardown cannot kill the work.
# When setsid is unavailable (notably minimal Termux), run the caller-provided
# function in the legacy disowned subshell instead of silently dropping work.
# Results are returned in SPAWN_DETACHED_PID and SPAWN_DETACHED_MODE.

spawn_detached() {
  local script="${1:-}" reentry_env="${2:-}" fallback_fn="${3:-}"
  shift 3 2>/dev/null || return 2

  if [ -n "$reentry_env" ] && [[ ! "$reentry_env" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    return 2
  fi
  if ! declare -F "$fallback_fn" >/dev/null 2>&1; then
    return 2
  fi

  if command -v setsid >/dev/null 2>&1 && [ -f "$script" ]; then
    if [ -n "$reentry_env" ]; then
      (
        export "$reentry_env=1"
        exec setsid bash "$script" "$@"
      ) </dev/null >/dev/null 2>&1 &
    else
      setsid bash "$script" "$@" </dev/null >/dev/null 2>&1 &
    fi
    SPAWN_DETACHED_MODE=setsid
  else
    ( "$fallback_fn" "$@" ) </dev/null >/dev/null 2>&1 &
    SPAWN_DETACHED_MODE=subshell
  fi

  SPAWN_DETACHED_PID=$!
  disown "$SPAWN_DETACHED_PID" 2>/dev/null || true
  return 0
}
