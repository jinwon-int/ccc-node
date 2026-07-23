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
