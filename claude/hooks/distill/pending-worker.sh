#!/usr/bin/env bash
# Detached shell scheduler target; Python owns claim, validation and completion.
set -uo pipefail
umask 077

PENDING_DIR="${1:-}"
PENDING_JOB="${2:-}"
DISTILL="${3:-}"
HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)" || exit 0
ADAPTER="$HOOKDIR/distill/pending_journal.py"
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
job_id="$(basename "$PENDING_JOB" .json)"

log() { printf '%s [pending-worker] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null; }

python3 "$ADAPTER" run "$PENDING_DIR" "$PENDING_JOB" "$DISTILL"
rc=$?
case "$rc" in
  0) log "pending completed job=$job_id" ;;
  75) log "pending skipped reason=job-lock-held job=$job_id" ;;
  74) log "pending retained reason=invalid-job job=$job_id" ;;
  *) log "pending retained reason=pipeline-failed job=$job_id" ;;
esac
exit 0
