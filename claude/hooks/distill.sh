#!/usr/bin/env bash
# Session Distiller — entry hook.
# Fired by PreCompact / SessionEnd / manual `/distill`.
# Pipeline: gather transcript -> redact -> Haiku extract (via `claude -p`, OAuth)
#           -> Honcho push (auto) + wiki-candidates queue (human-gated review).
#
# Design / decision: pages/team/dungae/DECISIONS.md [TM-1058], log [LOG-1212].
# Auth mode: OAuth via subprocess `claude -p` (Option B, no API key).
# Recursion guard: CLAUDE_DISTILL_INFLIGHT=1 short-circuits this script AND
# the other hooks (load-memory, load-tools, checkpoint, refresh-memory,
# evidence-gate) so the child Claude Code session does nothing extraneous.
#
# Safety:
#   - Always exit 0 (hook must never block parent).
#   - All external sends pass through redact pipeline.
#   - Off-switch: touch ~/.claude/state/distill.disabled
#   - Dry-run:   touch ~/.claude/state/distill.dryrun (no Honcho/queue writes)
set -uo pipefail

DISTILL_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || DISTILL_LIB_DIR="${HOME:-/root}/.claude/hooks"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$DISTILL_LIB_DIR/lib/hook-common.sh" || exit 0
# Fleet-wide autonomy guard (#386): one kill-switch/dry-run above this layer's
# own distill.disabled/distill.dryrun toggles. Sourced fail-open — a missing lib
# leaves ccc_autonomy_state undefined and distill behaves exactly as today.
# shellcheck source=claude/hooks/lib/autonomy-guard.sh
[ -r "$DISTILL_LIB_DIR/lib/autonomy-guard.sh" ] && . "$DISTILL_LIB_DIR/lib/autonomy-guard.sh" 2>/dev/null || true
wiki_memory_disabled() {
  [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ] || is_disabled "${CCC_WIKI_MEMORY_ENABLED:-1}"
}
honcho_memory_disabled() { is_disabled "${CCC_HONCHO_MEMORY_ENABLED:-1}"; }

# ---- recursion guard (FIRST line of executable logic) ----------------------
if [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ]; then
  exit 0
fi
case "${CCC_BRIDGE_DISTILL_MANAGED:-0}" in
  1|true|TRUE|yes|YES|on|ON) exit 0 ;;
esac

# ---- off-switch ------------------------------------------------------------
# State dir is overridable for testing / non-root installs (#73).
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
PENDING_DIR="$STATE_DIR/distill-pending"
umask 077
mkdir -p "$STATE_DIR" 2>/dev/null

if [ -f "$STATE_DIR/distill.disabled" ]; then
  printf '%s skipped reason=disabled trigger=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${1:-unknown}" 2>/dev/null >> "$LOG" || :
  exit 0
fi
# shellcheck source=claude/hooks/distill/provider-guard.sh
[ -r "$DISTILL_LIB_DIR/distill/provider-guard.sh" ] && . "$DISTILL_LIB_DIR/distill/provider-guard.sh" 2>/dev/null || true

# ---- fleet autonomy guard (#386) -------------------------------------------
# Resolved once, above this layer's own toggles, and honored on every entry path
# (foreground enqueue, bg re-entry, SessionStart pending-drain — all reach here).
#   kill    -> skip the whole distill: no extract LLM call, no local/external write.
#   dry-run -> force DRYRUN so the extract still stashes locally for debugging but
#              no Honcho/wiki/local-facts write happens (report-only).
# Fail-open: undefined guard (missing lib) => "active" => unchanged behavior.
AUTONOMY_STATE="active"
if declare -f ccc_autonomy_state >/dev/null 2>&1; then
  # Scope the lib's state-dir to distill's own STATE_DIR (the guard lib otherwise
  # honors CCC_CLAUDE_DIR, which distill.sh does not) so the autonomy.kill /
  # autonomy.dry-run files are read from the exact dir that holds distill's own
  # toggles — a mismatch would silently defeat the kill switch on non-root
  # installs. Env-var form (CCC_AUTONOMY=…) is unaffected. Local override only.
  AUTONOMY_STATE="$(CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_state 2>/dev/null || echo active)"
fi
if [ "$AUTONOMY_STATE" = "kill" ]; then
  printf '%s skipped reason=autonomy-kill trigger=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${1:-unknown}" 2>/dev/null >> "$LOG" || :
  # Record once per real trigger (foreground only) into the shared fleet ledger;
  # bg re-exec / pending-drain re-hit the same guard and must not double-log.
  if [ -z "${CLAUDE_DISTILL_BG:-}" ] && declare -f ccc_autonomy_record >/dev/null 2>&1; then
    CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_record distill kill "${1:-manual}"
  fi
  exit 0
fi

TRIGGER="${1:-manual}"   # precompact | sessionend | manual
DRYRUN=0
[ -f "$STATE_DIR/distill.dryrun" ] && DRYRUN=1
if [ "$AUTONOMY_STATE" = "dry-run" ]; then
  DRYRUN=1
  if [ -z "${CLAUDE_DISTILL_BG:-}" ] && declare -f ccc_autonomy_record >/dev/null 2>&1; then
    CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_record distill dry-run "$TRIGGER"
  fi
fi

# ts/log come from lib/hook-common.sh.

# ---- detached pipeline body --------------------------------------------------
# Shared by both spawn modes (setsid re-entry + legacy subshell fallback).
# All inputs come from CLAUDE_DISTILL_* env vars exported at the spawn site,
# so the function behaves identically however it is entered.
run_bg_pipeline() {
  # Ensure a valid CWD — A2A worker sessions run in /tmp dirs that may be
  # deleted before this bg process reaches `claude -p`, causing immediate
  # ENOENT exit (ec=1). Fall back to HOME so the CWD is always stable.
  cd "${HOME:-/root}" 2>/dev/null || cd / 2>/dev/null || true

  export CLAUDE_DISTILL_INFLIGHT=1
  local TRIGGER="${CLAUDE_DISTILL_TRIGGER:-manual}"
  local DRYRUN="${CLAUDE_DISTILL_DRYRUN:-0}"
  # Fleet dry-run stays authoritative even for a job enqueued before dry-run was
  # toggled and drained later — never downgrade a job's own dryrun, only raise.
  [ "${AUTONOMY_STATE:-active}" = "dry-run" ] && DRYRUN=1
  local HOOKDIR
  HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HOOKDIR=${HOME:-/root}/.claude/hooks
  # shellcheck source=claude/hooks/lib/mtime-prune.sh
  if [ -r "$HOOKDIR/lib/mtime-prune.sh" ]; then . "$HOOKDIR/lib/mtime-prune.sh"; fi

  local PIPE_START_EPOCH PIPE_PID
  PIPE_START_EPOCH="$(date -u +%s)"
  PIPE_PID="${BASHPID:-$$}"
  elapsed_s() { now="$(date -u +%s)"; printf '%s' "$((now - PIPE_START_EPOCH))"; }

  local EXTRACT_OUT ec cooldown_cls
  if declare -f ccc_distill_cooldown_class >/dev/null 2>&1; then
    cooldown_cls="$(ccc_distill_cooldown_class || true)"
    if [ -n "${cooldown_cls:-}" ]; then
      log "extract skipped reason=provider-cooldown class=$cooldown_cls trigger=$TRIGGER pid=$PIPE_PID"
      return 1
    fi
  fi
  EXTRACT_OUT="$(bash "$HOOKDIR/distill/extract.sh" 2>>"$LOG")"
  ec=$?
  if [ $ec -ne 0 ] || [ -z "$EXTRACT_OUT" ]; then
    log "extract failed ec=$ec trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
    return 1
  fi

  # Stash extracted JSON for debugging + sub-script consumption.
  local STASH="$STATE_DIR/distill-last.json"
  local STASH_DIR="$STATE_DIR/distill-history"
  local HISTORY_KEEP="${CCC_DISTILL_HISTORY_KEEP:-20}"
  case "$HISTORY_KEEP" in ''|*[!0-9]*) HISTORY_KEEP=20 ;; esac
  if [ -f "$STASH" ]; then
    mkdir -p "$STASH_DIR" 2>/dev/null
    cp -p "$STASH" "$STASH_DIR/$(date -u +%Y%m%d-%H%M%S)-${BASHPID:-$$}.json" 2>/dev/null || true
  fi
  printf '%s' "$EXTRACT_OUT" > "$STASH" 2>/dev/null
  if [ "$HISTORY_KEEP" -gt 0 ]; then
    # Portable, whitespace-safe prune (busybox find has no -printf; see #449).
    if declare -F prune_keep_newest >/dev/null 2>&1; then
      prune_keep_newest "$STASH_DIR" '*.json' "$HISTORY_KEEP"
    fi
  fi

  if [ "$DRYRUN" = "1" ]; then
    log "dry-run skipping local/honcho/wiki writes (see $STASH) trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
    return 0
  fi

  # One provider-neutral local-memory transaction owns resume + facts together.
  # It persists owner-only pre-images and a body-free rollback head before
  # returning success; no external sink call runs while its lock is held.
  python3 "$HOOKDIR/distill/local-memory-commit.py" --mode both < "$STASH" >> "$LOG" 2>&1 || \
    log "local-memory-commit non-zero"

  if honcho_memory_disabled; then
    log "honcho-push skipped reason=disabled"
  else
    bash "$HOOKDIR/distill/honcho-push.sh" < "$STASH" >> "$LOG" 2>&1 || \
      log "honcho-push non-zero (queued for retry)"
  fi
  if wiki_memory_disabled; then
    log "wiki-queue skipped reason=disabled"
  else
    bash "$HOOKDIR/distill/wiki-queue.sh" < "$STASH" >> "$LOG" 2>&1 || \
      log "wiki-queue non-zero"
  fi
  log "done trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
  return 0
}

# ---- bg re-entry (setsid-detached pipeline; spawned at the bottom) -----------
# Reached only when the spawn site re-invokes this script with
# CLAUDE_DISTILL_BG=1 and WITHOUT CLAUDE_DISTILL_INFLIGHT (run_bg_pipeline
# sets INFLIGHT itself for the nested `claude -p` session), so the recursion
# guard at the top does not short-circuit this path.
if [ "${CLAUDE_DISTILL_BG:-}" = "1" ]; then
  if [ "${CCC_PENDING_JOURNAL_MANAGED:-}" = "1" ]; then
    # Exit 76 is the private success/completion token. The Python claimant is
    # the only layer allowed to turn it into durable record removal.
    run_bg_pipeline && exit 76
    exit 1
  fi

  run_bg_pipeline || true
  exit 0
fi

encode_project_dir() { printf '%s' "$1" | sed -E 's|[^A-Za-z0-9_]|-|g'; }
legacy_project_dir() { printf '%s' "$1" | sed 's|/|-|g'; }

scope_values() {
  [ -n "${CCC_DISTILL_SCOPE_CWDS:-}" ] && printf '%s\n' "$CCC_DISTILL_SCOPE_CWDS" | tr ',:' '\n'
  [ -f "$STATE_DIR/distill.scope" ] && cat "$STATE_DIR/distill.scope"
}

scope_allows_project() {
  local project="$1" cwd="$2" raw val enc legacy any=0
  while IFS= read -r raw; do
    val="$(printf '%s' "$raw" | sed -E 's/#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -z "$val" ] && continue
    any=1
    [ "$val" = "$cwd" ] && return 0
    [ "$val" = "$project" ] && return 0
    enc="$(encode_project_dir "$val")"
    legacy="$(legacy_project_dir "$val")"
    [ "$project" = "$enc" ] && return 0
    [ "$project" = "$legacy" ] && return 0
  done < <(scope_values)
  [ "$any" = "0" ] && return 0
  return 1
}

log "start trigger=$TRIGGER dryrun=$DRYRUN pid=$$"

# ---- read hook stdin payload (PreCompact/SessionEnd give JSON, manual = empty)
HOOK_INPUT="$(cat 2>/dev/null || true)"
SESSION_ID="$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$HOOK_INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"
SOURCE_CWD="$(printf '%s' "$HOOK_INPUT" | jq -r '.cwd // .workspace.current_dir // .workspace.cwd // empty' 2>/dev/null)"
PROJECT_ENC=""

# Fallback: find the most-recent transcript jsonl for cwd-encoded project dir.
# Uses CLAUDE_PROJECTS_DIR (default $HOME/.claude/projects) so non-root
# installs (e.g. /opt/ccc-node on nosuk/soonwook/dungae) work out of the box.
PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-${HOME:-/root}/.claude/projects}"
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  for PROJ_ENC in "$(encode_project_dir "${PWD:-/root}")" "$(legacy_project_dir "${PWD:-/root}")"; do
    TRANSCRIPT_PATH="$(ls -t "$PROJECTS_DIR/$PROJ_ENC"/*.jsonl 2>/dev/null | head -1)"
    [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] && break
  done
fi

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  log "skip reason=no-transcript trigger=$TRIGGER pid=$$"
  # ^ trigger=/pid= kept after the semantic fields (reason=, cwd=, turns=)
  # so existing log-parsers and tests that grep "skip reason=…<semantic>"
  # substrings keep working — see distill-scope.test.sh.
  exit 0
fi

PROJECT_ENC="$(basename "$(dirname "$TRANSCRIPT_PATH")")"
if [ -z "$SOURCE_CWD" ]; then
  if [ "$PROJECT_ENC" = "$(encode_project_dir "${PWD:-/root}")" ] || [ "$PROJECT_ENC" = "$(legacy_project_dir "${PWD:-/root}")" ]; then
    SOURCE_CWD="${PWD:-/root}"
  else
    SOURCE_CWD="encoded:$PROJECT_ENC"
  fi
fi

[ -z "$SESSION_ID" ] && SESSION_ID="$(basename "$TRANSCRIPT_PATH" .jsonl)"
log "transcript=$TRANSCRIPT_PATH session=$SESSION_ID source_cwd=$SOURCE_CWD source_project=$PROJECT_ENC"

if ! scope_allows_project "$PROJECT_ENC" "$SOURCE_CWD"; then
  log "skip reason=cwd-out-of-scope cwd=$SOURCE_CWD project=$PROJECT_ENC trigger=$TRIGGER pid=$$"
  exit 0
fi

# ---- min-content gate (skip trivial sessions) ------------------------------
# Sanitize BEFORE the tail call (skill-review.sh's order): a malformed
# CCC_DISTILL_TURN_WINDOW used to reach `tail -n` first, error it, read
# TURNS=0, and silently skip every distill as too-few-turns.
MIN_TURNS="${CCC_DISTILL_MIN_TURNS:-3}"
TURN_WINDOW="${CCC_DISTILL_TURN_WINDOW:-400}"
case "$MIN_TURNS" in ''|*[!0-9]*) MIN_TURNS=3 ;; esac
case "$TURN_WINDOW" in ''|*[!0-9]*) TURN_WINDOW=400 ;; esac
TURNS="$(tail -n "$TURN_WINDOW" "$TRANSCRIPT_PATH" 2>/dev/null \
  | jq -r 'select(.type == "user" or .type == "assistant") | .type' 2>/dev/null \
  | wc -l | tr -d '[:space:]')"
case "$TURNS" in ''|*[!0-9]*) TURNS=0 ;; esac
if [ "$TURNS" -lt "$MIN_TURNS" ]; then
  log "skip reason=too-few-turns turns=$TURNS min_turns=$MIN_TURNS trigger=$TRIGGER pid=$$"
  exit 0
fi

# ---- fire pipeline (detach so hook returns fast; SessionEnd has tight timeout)
# Inputs for run_bg_pipeline — exported so both spawn modes (setsid re-entry
# and subshell fallback) read the same contract.
export CLAUDE_DISTILL_TRIGGER="$TRIGGER"
export CLAUDE_DISTILL_SESSION="$SESSION_ID"
export CLAUDE_DISTILL_TRANSCRIPT="$TRANSCRIPT_PATH"
export CLAUDE_DISTILL_SOURCE_CWD="$SOURCE_CWD"
export CLAUDE_DISTILL_SOURCE_PROJECT="$PROJECT_ENC"
export CLAUDE_DISTILL_DRYRUN="$DRYRUN"

# Python is the sole owner of queue serialization, ID derivation and dedup.
PENDING_ADAPTER="$DISTILL_LIB_DIR/distill/pending_journal.py"
if [ ! -r "$PENDING_ADAPTER" ]; then
  log "enqueue failed reason=missing-journal-adapter"
  exit 0
fi
ENQUEUE_RESULT="$(python3 "$PENDING_ADAPTER" enqueue "$PENDING_DIR" 2>>"$LOG")" || {
  log "enqueue failed reason=journal"
  exit 0
}
JOB_ID="$(printf '%s' "$ENQUEUE_RESULT" | jq -r '.job_id // empty' 2>/dev/null)"
ENQUEUE_CREATED="$(printf '%s' "$ENQUEUE_RESULT" | jq -r '.created // false' 2>/dev/null)"
case "$JOB_ID" in ''|*[!0-9a-f]*) log "enqueue failed reason=journal-result"; exit 0 ;; esac
PENDING_JOB="$PENDING_DIR/$JOB_ID.json"
if [ "$ENQUEUE_CREATED" = "true" ]; then
  log "enqueued job=$JOB_ID trigger=$TRIGGER"
else
  log "enqueue dedup job=$JOB_ID trigger=$TRIGGER"
fi
export CLAUDE_DISTILL_JOB="$PENDING_JOB"

if declare -f ccc_distill_cooldown_class >/dev/null 2>&1; then
  _cd_cls="$(ccc_distill_cooldown_class || true)"
  if [ -n "${_cd_cls:-}" ]; then
    log "skip spawn reason=provider-cooldown class=$_cd_cls job=$JOB_ID"
    exit 0
  fi
fi

# Prefer `setsid`: a plain disowned subshell stays in the hook's process
# group/session, so when the parent session is torn down as a group (ssh-driven
# maintenance sessions, CLI teardown) the pipeline dies silently before logging
# anything — observed fleet-wide on 2026-07-07. The shared helper keeps the
# legacy subshell fallback for environments without setsid.
DISTILL_HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
DISTILL_SELF="$DISTILL_HOOKDIR/$(basename "${BASH_SOURCE[0]:-$0}")"
PENDING_WORKER="$DISTILL_HOOKDIR/distill/pending-worker.sh"
run_pending_job() { bash "$PENDING_WORKER" "$PENDING_DIR" "$PENDING_JOB" "$DISTILL_SELF"; }
# shellcheck source=claude/hooks/lib/spawn-detached.sh
if [ -n "$DISTILL_HOOKDIR" ] && [ -r "$PENDING_WORKER" ] \
  && [ -r "$DISTILL_HOOKDIR/lib/spawn-detached.sh" ]; then
  . "$DISTILL_HOOKDIR/lib/spawn-detached.sh"
  if spawn_detached "$PENDING_WORKER" "" run_pending_job \
    "$PENDING_DIR" "$PENDING_JOB" "$DISTILL_SELF"; then
    log "spawned bg pid=$SPAWN_DETACHED_PID mode=$SPAWN_DETACHED_MODE"
  else
    log "spawn failed reason=invalid-detached-contract"
  fi
else
  log "spawn failed reason=missing-detached-helper"
fi
exit 0
