#!/usr/bin/env bash
# autonomy-guard.sh — fleet-wide kill-switch / dry-run for autonomous writes (#386).
#
# Source this from any autonomous-write layer (skill-autosave, distill, …). It
# resolves ONE global autonomy state that sits ABOVE each layer's own mode, so a
# single switch can halt or mute every no-approval write at once. Defines
# functions only; no side effects.
#
# State (highest precedence first):
#   CCC_AUTONOMY=kill|off        -> "kill"     (do nothing)
#   CCC_AUTONOMY=dry-run|dry     -> "dry-run"  (report only, write nothing)
#   CCC_AUTONOMY=active|on|''    -> fall through to the file switches
#   $STATE_DIR/autonomy.kill     -> "kill"
#   $STATE_DIR/autonomy.dry-run  -> "dry-run"
#   (otherwise)                  -> "active"
#
# The default is "active" so existing nodes are unchanged until an operator opts
# into kill/dry-run.

ccc_autonomy_state_dir() {
  printf '%s' "${CCC_STATE_DIR:-${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}/state}"
}

ccc_autonomy_state() {
  case "${CCC_AUTONOMY:-}" in
    kill|killed|off|OFF) printf 'kill'; return 0 ;;
    dry-run|dryrun|dry|DRY) printf 'dry-run'; return 0 ;;
    active|on|ON|'') : ;;                 # fall through to file switches
    *) : ;;                               # unknown spelling -> treat as active
  esac
  local dir; dir="$(ccc_autonomy_state_dir)"
  if [ -f "$dir/autonomy.kill" ]; then printf 'kill'; return 0; fi
  if [ -f "$dir/autonomy.dry-run" ]; then printf 'dry-run'; return 0; fi
  printf 'active'
}

# ccc_autonomy_record LAYER STATE [DETAIL] — append one body-free line to a
# shared, fleet-wide autonomy ledger so an operator can see, in one place, what
# the kill/dry-run switch actually stopped or gated across every layer (each
# layer otherwise logs only to its own file). Owner-only, bounded, fail-open:
# any error is swallowed and the caller's exit status is never affected, so it
# is safe to call on a hot skip path. Body-free by construction — only the layer
# name, the resolved state, and a short sanitized detail (e.g. a trigger) are
# stored; never a transcript, prompt, command, or payload.
#
# Ledger path: $STATE_DIR/autonomy-ledger.jsonl  (newest CCC_AUTONOMY_LEDGER_MAX
# lines kept, default 500). Runs in a subshell so umask/`set` changes cannot
# leak into the caller.
ccc_autonomy_record() {
  local layer="${1:-unknown}" state="${2:-unknown}" detail="${3:-}"
  # Restrict to safe charsets and cap length so the printf'd JSON can never be
  # broken by an unexpected value (fields are code-controlled, but defensive).
  layer="$(printf '%s' "$layer"  | tr -cd 'A-Za-z0-9._-'      | cut -c1-40)"
  state="$(printf '%s' "$state"  | tr -cd 'A-Za-z0-9-'        | cut -c1-16)"
  detail="$(printf '%s' "$detail" | tr -cd 'A-Za-z0-9._:=/ -' | cut -c1-80)"
  (
    umask 077
    local dir file lock ts max n tmp
    dir="$(ccc_autonomy_state_dir)"
    file="$dir/autonomy-ledger.jsonl"
    lock="$file.lock"
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || ts=""
    mkdir -p "$dir" 2>/dev/null || exit 0
    # Serialize append+trim across layers (they all write this one file). The
    # trim below is read-then-rename; without a lock a concurrent append landing
    # between the snapshot and the mv would be clobbered — the newest record, the
    # one the bound is meant to keep. Hold an exclusive lock for both the append
    # and the trim so they act as one unit. Best-effort: if flock is unavailable
    # we proceed unlocked (append is atomic; only the rare trim race remains).
    exec 9>>"$lock" 2>/dev/null && flock 9 2>/dev/null || true
    printf '{"ts":"%s","layer":"%s","state":"%s","detail":"%s"}\n' \
      "$ts" "$layer" "$state" "$detail" >> "$file" 2>/dev/null || exit 0
    chmod 600 "$file" 2>/dev/null || true
    max="${CCC_AUTONOMY_LEDGER_MAX:-500}"
    case "$max" in ''|*[!0-9]*) max=500 ;; esac
    n="$(wc -l < "$file" 2>/dev/null | tr -d '[:space:]')" || n=0
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    if [ "$n" -gt "$max" ]; then
      tmp="$file.tmp.$$"
      if tail -n "$max" "$file" > "$tmp" 2>/dev/null; then
        chmod 600 "$tmp" 2>/dev/null || true
        mv "$tmp" "$file" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
      else
        rm -f "$tmp" 2>/dev/null || true
      fi
    fi
  ) 2>/dev/null || true
  return 0
}
