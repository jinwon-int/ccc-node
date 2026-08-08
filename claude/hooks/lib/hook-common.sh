#!/usr/bin/env bash
# hook-common.sh — shared helpers for ccc-node hook scripts.
#
# Source this from a hook; it defines functions only and has no side effects.
# Consumers resolve it relative to their own file so the same script works
# from a repo checkout (tests) and from the deployed ~/.claude/hooks tree.
#
# Canonical home of helpers that were previously copy-pasted per hook
# (is_disabled x6, ts/log x5, find_memory_tool x2 — epic #584 P0-3).

# is_disabled <value> — true when the value is an explicit "off" spelling.
is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }

# ts — UTC ISO-8601 timestamp.
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# log <msg...> — best-effort append to $LOG (evaluated at call time).
#
# `2>/dev/null` precedes the append: redirections are applied left to right, so
# with `>> "$LOG" 2>/dev/null` the shell's "No such file or directory" for an
# unwritable destination was emitted before stderr was silenced and leaked to
# the hook's own stderr. `|| :` keeps the documented best-effort contract --
# hooks call this fire-and-forget, several as the last statement of a function,
# where a failed append would otherwise become that function's exit status.
log() { printf '%s %s\n' "$(ts)" "$*" 2>/dev/null >> "${LOG:-/dev/null}" || :; }

# find_memory_tool <tool-name> — locate a memory CLI next to the hooks or in
# the repo scripts/ dir. Requires $HOOKDIR to be set by the caller.
find_memory_tool() { # <tool-name>
  local name="$1" d
  for d in "${CCC_MEMORY_TOOLS_DIR:-}" "${HOOKDIR:-}" "${HOOKDIR:-}/../../scripts"; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
  done
  return 1
}
