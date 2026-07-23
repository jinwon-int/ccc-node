#!/usr/bin/env bash
# SessionStart recovery launcher for durable distill jobs.
#
# The launcher is bounded and fail-open: it starts at most MAX_BATCH detached
# workers, never waits for provider I/O, and leaves every job on disk until its
# worker completes successfully. Per-job locking in distill.sh prevents a live
# SessionEnd worker and a recovery worker from processing the same job at once.
set -uo pipefail

unset CLAUDE_DISTILL_INFLIGHT
umask 077

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
PENDING_DIR="$STATE_DIR/distill-pending"
MAX_BATCH="${CCC_DISTILL_PENDING_DRAIN_BATCH:-3}"

case "$MAX_BATCH" in ''|*[!0-9]*) MAX_BATCH=3 ;; esac
[ "$MAX_BATCH" -gt 0 ] || exit 0
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

log() { printf '%s [pending-drain] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null; }

[ -d "$PENDING_DIR" ] || exit 0
[ ! -L "$PENDING_DIR" ] || { log "skip reason=pending-dir-symlink"; exit 0; }
[ ! -f "$STATE_DIR/distill.disabled" ] || { log "skip reason=distill-disabled"; exit 0; }

HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)" || HOOKDIR="${HOME:-/root}/.claude/hooks"
DISTILL="$HOOKDIR/distill.sh"
SPAWN_HELPER="$HOOKDIR/lib/spawn-detached.sh"
[ -f "$DISTILL" ] && [ -r "$SPAWN_HELPER" ] || { log "skip reason=missing-runtime"; exit 0; }

# shellcheck source=claude/hooks/lib/spawn-detached.sh
. "$SPAWN_HELPER"

# Fleet autonomy guard (#386): under kill, drain nothing. Each worker would only
# re-exec distill.sh and exit at its own kill guard, so skip the pointless
# spawns entirely — completing the "kill halts everything" contract. dry-run
# proceeds (each job's distill.sh forces DRYRUN and writes nothing external).
# Fail-open: missing lib => active. Scope the lib's state dir to this launcher's
# STATE_DIR so it reads the same autonomy.kill file distill uses. No ledger
# record here (SessionStart fires often; the primary decision points already
# record) — a distill.log line mirrors the distill.disabled short-circuit above.
if [ -r "$HOOKDIR/lib/autonomy-guard.sh" ]; then
  # shellcheck source=claude/hooks/lib/autonomy-guard.sh
  . "$HOOKDIR/lib/autonomy-guard.sh" 2>/dev/null || true
fi
if declare -f ccc_autonomy_state >/dev/null 2>&1 \
  && [ "$(CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_state 2>/dev/null || echo active)" = kill ]; then
  log "skip reason=autonomy-kill"
  exit 0
fi

run_pending_job() {
  # SessionStart's Honcho drain uses CLAUDE_DISTILL_INFLIGHT, but this recovery
  # launcher must re-enter distill.sh before that guard. Remove any inherited
  # value defensively; run_bg_pipeline sets it again before invoking Claude.
  env -u CLAUDE_DISTILL_INFLIGHT \
    CLAUDE_DISTILL_BG=1 CLAUDE_DISTILL_JOB="${CLAUDE_DISTILL_JOB:?}" \
    bash "$DISTILL" recovery
}

started=0
while IFS= read -r job; do
  [ "$started" -lt "$MAX_BATCH" ] || break
  [ -f "$job" ] && [ ! -L "$job" ] || continue
  export CLAUDE_DISTILL_JOB="$job"
  if spawn_detached "$DISTILL" CLAUDE_DISTILL_BG run_pending_job recovery; then
    log "spawned job=$(basename "$job" .json) pid=$SPAWN_DETACHED_PID mode=$SPAWN_DETACHED_MODE"
    started=$((started + 1))
  else
    log "spawn failed job=$(basename "$job" .json)"
  fi
done < <(find "$PENDING_DIR" -maxdepth 1 -type f -name '*.json' -print 2>/dev/null | sort)

[ "$started" -eq 0 ] || log "started=$started"
exit 0
