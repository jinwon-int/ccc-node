#!/usr/bin/env bash
# SessionStart recovery launcher for durable distill jobs.
#
# The launcher is bounded and fail-open: it starts at most MAX_BATCH detached
# workers, never waits for provider I/O, and leaves every job on disk until its
# worker completes successfully. Per-job locking in distill.sh prevents a live
# SessionEnd worker and a recovery worker from processing the same job at once.
#
# Recursion guard: extract/recovery children export CLAUDE_DISTILL_INFLIGHT=1.
# Honor that flag and exit before unsetting it. Unsetting first made every
# child SessionStart re-enter this launcher (sogyo 2026-08-23 storm).
set -uo pipefail

umask 077

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
PENDING_DIR="$STATE_DIR/distill-pending"
MAX_BATCH="${CCC_DISTILL_PENDING_DRAIN_BATCH:-3}"
MAX_INFLIGHT="${CCC_DISTILL_PENDING_INFLIGHT_MAX:-3}"

case "$MAX_BATCH" in ''|*[!0-9]*) MAX_BATCH=3 ;; esac
case "$MAX_INFLIGHT" in ''|*[!0-9]*) MAX_INFLIGHT=3 ;; esac
[ "$MAX_BATCH" -gt 0 ] || exit 0
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

log() { printf '%s [pending-drain] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" 2>/dev/null >> "$LOG" || :; }

if [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ]; then
  log "skip reason=distill-inflight"
  exit 0
fi

# Workers must not inherit a stray inflight flag (distill.sh short-circuits).
unset CLAUDE_DISTILL_INFLIGHT

[ -d "$PENDING_DIR" ] || exit 0
[ ! -L "$PENDING_DIR" ] || { log "skip reason=pending-dir-symlink"; exit 0; }
[ ! -f "$STATE_DIR/distill.disabled" ] || { log "skip reason=distill-disabled"; exit 0; }

HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)" || HOOKDIR="${HOME:-/root}/.claude/hooks"
DISTILL="$HOOKDIR/distill.sh"
PENDING_WORKER="$HOOKDIR/distill/pending-worker.sh"
PENDING_ADAPTER="$HOOKDIR/distill/pending_journal.py"
SPAWN_HELPER="$HOOKDIR/lib/spawn-detached.sh"
[ -f "$DISTILL" ] && [ -r "$PENDING_WORKER" ] \
  && [ -r "$PENDING_ADAPTER" ] && [ -r "$SPAWN_HELPER" ] \
  || { log "skip reason=missing-runtime"; exit 0; }

# shellcheck source=claude/hooks/distill/provider-guard.sh
[ -r "$HOOKDIR/distill/provider-guard.sh" ] && . "$HOOKDIR/distill/provider-guard.sh" 2>/dev/null || true
if declare -f ccc_distill_cooldown_class >/dev/null 2>&1; then
  _cd_cls="$(ccc_distill_cooldown_class || true)"
  if [ -n "${_cd_cls:-}" ]; then
    log "skip reason=provider-cooldown class=$_cd_cls"
    exit 0
  fi
fi

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
  bash "$PENDING_WORKER" "${1:?}" "${2:?}" "${3:?}"
}

# Load gate (#1250): never add extraction workers to a saturated node. sogyo
# 2026-08-23 kept draining at load 98 / swap 0 until sshd could no longer fork.
# Fail-open when /proc is unreadable: pseudo-fs reads do not degrade under the
# saturation this gate guards against, and an unreadable source must not halt
# recovery on healthy nodes.
LOADAVG_PATH="${CCC_DISTILL_PENDING_LOADAVG_PATH:-/proc/loadavg}"
MEMINFO_PATH="${CCC_DISTILL_PENDING_MEMINFO_PATH:-/proc/meminfo}"
LOAD_FACTOR="${CCC_DISTILL_PENDING_LOAD_FACTOR:-2}"
MIN_MEM_KB="${CCC_DISTILL_PENDING_MIN_MEM_KB:-262144}"
MIN_SWAP_FREE_PCT="${CCC_DISTILL_PENDING_MIN_SWAP_FREE_PCT:-10}"
case "$LOAD_FACTOR" in ''|*[!0-9.]*) LOAD_FACTOR=2 ;; esac
case "$MIN_MEM_KB" in ''|*[!0-9]*) MIN_MEM_KB=262144 ;; esac
case "$MIN_SWAP_FREE_PCT" in ''|*[!0-9]*) MIN_SWAP_FREE_PCT=10 ;; esac
if [ -r "$LOADAVG_PATH" ] && [ -r "$MEMINFO_PATH" ]; then
  load1="$(awk '{print $1}' "$LOADAVG_PATH" 2>/dev/null)"
  nproc_now="${CCC_DISTILL_PENDING_NPROC:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')}"
  case "$nproc_now" in ''|*[!0-9]*) nproc_now=1 ;; esac
  mem_avail_kb="$(awk '/^MemAvailable:/ {print $2; exit}' "$MEMINFO_PATH" 2>/dev/null)"
  swap_total_kb="$(awk '/^SwapTotal:/ {print $2; exit}' "$MEMINFO_PATH" 2>/dev/null)"
  swap_free_kb="$(awk '/^SwapFree:/ {print $2; exit}' "$MEMINFO_PATH" 2>/dev/null)"
  gate_reason=""
  if awk -v l="${load1:-0}" -v n="$nproc_now" -v f="$LOAD_FACTOR" 'BEGIN{exit !(l+0 >= n*f)}'; then
    gate_reason="load1=${load1:-?} nproc=$nproc_now factor=$LOAD_FACTOR"
  elif [ -n "$mem_avail_kb" ] && [ "$mem_avail_kb" -lt "$MIN_MEM_KB" ]; then
    gate_reason="mem_available_kb=$mem_avail_kb min=$MIN_MEM_KB"
  elif [ -n "$swap_total_kb" ] && [ "$swap_total_kb" -gt 0 ] && [ -n "$swap_free_kb" ] \
    && [ $((swap_free_kb * 100)) -lt $((swap_total_kb * MIN_SWAP_FREE_PCT)) ]; then
    gate_reason="swap_free_kb=$swap_free_kb min_pct=$MIN_SWAP_FREE_PCT"
  fi
  if [ -n "$gate_reason" ]; then
    log "skip reason=load-gate $gate_reason"
    exit 0
  fi
fi

# Global cap across concurrent SessionStarts. Held claim locks == live workers
# in this STATE_DIR (flock releases on death, so stale files do not count).
held=0
shopt -s nullglob
for lock in "$PENDING_DIR"/*.json.lock; do
  [ -e "$lock" ] || continue
  if ! flock -n "$lock" true 2>/dev/null; then
    held=$((held + 1))
  fi
done
shopt -u nullglob
slots=$((MAX_INFLIGHT - held))
if [ "$slots" -le 0 ]; then
  log "skip reason=inflight-cap held=$held max=$MAX_INFLIGHT"
  exit 0
fi
[ "$MAX_BATCH" -le "$slots" ] || MAX_BATCH="$slots"

started=0
while IFS= read -r job; do
  [ "$started" -lt "$MAX_BATCH" ] || break
  [ -f "$job" ] && [ ! -L "$job" ] || continue
  if spawn_detached "$PENDING_WORKER" "" run_pending_job \
    "$PENDING_DIR" "$job" "$DISTILL"; then
    log "spawned job=$(basename "$job" .json) pid=$SPAWN_DETACHED_PID mode=$SPAWN_DETACHED_MODE"
    started=$((started + 1))
  else
    log "spawn failed job=$(basename "$job" .json)"
  fi
done < <(python3 "$PENDING_ADAPTER" discover "$PENDING_DIR" --limit "$MAX_BATCH" 2>>"$LOG")

[ "$started" -eq 0 ] || log "started=$started"
exit 0
