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

is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }
wiki_memory_disabled() {
  [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ] || is_disabled "${CCC_WIKI_MEMORY_ENABLED:-1}"
}

# ---- recursion guard (FIRST line of executable logic) ----------------------
if [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ]; then
  exit 0
fi

# ---- off-switch ------------------------------------------------------------
# State dir is overridable for testing / non-root installs (#73).
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
PENDING_DIR="$STATE_DIR/distill-pending"
umask 077
mkdir -p "$STATE_DIR" 2>/dev/null

if [ -f "$STATE_DIR/distill.disabled" ]; then
  printf '%s skipped reason=disabled trigger=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${1:-unknown}" >> "$LOG" 2>/dev/null
  exit 0
fi

TRIGGER="${1:-manual}"   # precompact | sessionend | manual
DRYRUN=0
[ -f "$STATE_DIR/distill.dryrun" ] && DRYRUN=1

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG" 2>/dev/null; }

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
  local HOOKDIR
  HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HOOKDIR=${HOME:-/root}/.claude/hooks
  # shellcheck source=claude/hooks/lib/mtime-prune.sh
  if [ -r "$HOOKDIR/lib/mtime-prune.sh" ]; then . "$HOOKDIR/lib/mtime-prune.sh"; fi

  local PIPE_START_EPOCH PIPE_PID
  PIPE_START_EPOCH="$(date -u +%s)"
  PIPE_PID="${BASHPID:-$$}"
  elapsed_s() { now="$(date -u +%s)"; printf '%s' "$((now - PIPE_START_EPOCH))"; }

  local EXTRACT_OUT ec
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
  bash "$HOOKDIR/distill/resume-write.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "resume-write non-zero"
  if [ "$HISTORY_KEEP" -gt 0 ]; then
    # Portable, whitespace-safe prune (busybox find has no -printf; see #449).
    if declare -F prune_keep_newest >/dev/null 2>&1; then
      prune_keep_newest "$STASH_DIR" '*.json' "$HISTORY_KEEP"
    fi
  fi

  if [ "$DRYRUN" = "1" ]; then
    log "dry-run skipping honcho/wiki push (see $STASH) trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
    return 0
  fi

  bash "$HOOKDIR/distill/honcho-push.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "honcho-push non-zero (queued for retry)"
  if wiki_memory_disabled; then
    log "wiki-queue skipped reason=disabled"
  else
    bash "$HOOKDIR/distill/wiki-queue.sh" < "$STASH" >> "$LOG" 2>&1 || \
      log "wiki-queue non-zero"
  fi
  bash "$HOOKDIR/distill/local-facts.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "local-facts non-zero"

  log "done trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
  return 0
}

# ---- bg re-entry (setsid-detached pipeline; spawned at the bottom) -----------
# Reached only when the spawn site re-invokes this script with
# CLAUDE_DISTILL_BG=1 and WITHOUT CLAUDE_DISTILL_INFLIGHT (run_bg_pipeline
# sets INFLIGHT itself for the nested `claude -p` session), so the recursion
# guard at the top does not short-circuit this path.
if [ "${CLAUDE_DISTILL_BG:-}" = "1" ]; then
  PENDING_JOB="${CLAUDE_DISTILL_JOB:-}"
  if [ -n "$PENDING_JOB" ]; then
    case "$PENDING_JOB" in
      "$PENDING_DIR"/*.json) ;;
      *) log "pending rejected reason=path-outside-queue"; exit 0 ;;
    esac
    if [ ! -f "$PENDING_JOB" ] || [ -L "$PENDING_JOB" ]; then
      log "pending skipped reason=missing-or-symlink"
      exit 0
    fi

    # Per-job advisory lock. A killed worker releases the lock while leaving
    # the durable JSON job in place for the next SessionStart recovery pass.
    if [ -L "$PENDING_JOB.lock" ]; then
      log "pending rejected reason=lock-symlink job=$(basename "$PENDING_JOB" .json)"
      exit 0
    fi
    exec 8>"$PENDING_JOB.lock"
    flock -n 8 || { log "pending skipped reason=job-lock-held job=$(basename "$PENDING_JOB" .json)"; exit 0; }

    CLAUDE_DISTILL_TRIGGER="$(jq -r '.trigger // "manual"' "$PENDING_JOB" 2>/dev/null)"
    CLAUDE_DISTILL_SESSION="$(jq -r '.session_id // empty' "$PENDING_JOB" 2>/dev/null)"
    CLAUDE_DISTILL_TRANSCRIPT="$(jq -r '.transcript_path // empty' "$PENDING_JOB" 2>/dev/null)"
    CLAUDE_DISTILL_SOURCE_CWD="$(jq -r '.source_cwd // empty' "$PENDING_JOB" 2>/dev/null)"
    CLAUDE_DISTILL_SOURCE_PROJECT="$(jq -r '.source_project // empty' "$PENDING_JOB" 2>/dev/null)"
    CLAUDE_DISTILL_DRYRUN="$(jq -r '.dryrun // 0' "$PENDING_JOB" 2>/dev/null)"
    CCC_NODE_ISOLATION_PROFILE="$(jq -r '.isolation_profile // "fleet"' "$PENDING_JOB" 2>/dev/null)"
    CCC_WIKI_MEMORY_ENABLED="$(jq -r '.wiki_memory_enabled // "1"' "$PENDING_JOB" 2>/dev/null)"
    CCC_MEMORY_USER_LABEL="$(jq -r '.memory_user_label // "Seo Jin On / 서진원"' "$PENDING_JOB" 2>/dev/null)"
    CCC_MEMORY_ASSISTANT_LABEL="$(jq -r '.memory_assistant_label // "dungae, a Hermes Team2 worker"' "$PENDING_JOB" 2>/dev/null)"
    export CLAUDE_DISTILL_TRIGGER CLAUDE_DISTILL_SESSION CLAUDE_DISTILL_TRANSCRIPT
    export CLAUDE_DISTILL_SOURCE_CWD CLAUDE_DISTILL_SOURCE_PROJECT CLAUDE_DISTILL_DRYRUN
    export CCC_NODE_ISOLATION_PROFILE CCC_WIKI_MEMORY_ENABLED
    export CCC_MEMORY_USER_LABEL CCC_MEMORY_ASSISTANT_LABEL

    if [ -z "$CLAUDE_DISTILL_SESSION" ] || [ ! -f "$CLAUDE_DISTILL_TRANSCRIPT" ]; then
      log "pending retained reason=invalid-job job=$(basename "$PENDING_JOB" .json)"
      exit 0
    fi

    if run_bg_pipeline; then
      job_id="$(basename "$PENDING_JOB" .json)"
      rm -f "$PENDING_JOB" 2>/dev/null || {
        log "pending retained reason=remove-failed job=$job_id"
        exit 0
      }
      rm -f "$PENDING_JOB.lock" 2>/dev/null || true
      command -v sync >/dev/null 2>&1 && sync -f "$PENDING_DIR" 2>/dev/null || true
      log "pending completed job=$job_id"
    else
      log "pending retained reason=pipeline-failed job=$(basename "$PENDING_JOB" .json)"
    fi
    exit 0
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
MIN_TURNS="${CCC_DISTILL_MIN_TURNS:-3}"
TURN_WINDOW="${CCC_DISTILL_TURN_WINDOW:-400}"
TURNS="$(tail -n "$TURN_WINDOW" "$TRANSCRIPT_PATH" 2>/dev/null \
  | jq -r 'select(.type == "user" or .type == "assistant") | .type' 2>/dev/null \
  | wc -l | tr -d '[:space:]')"
case "$MIN_TURNS" in ''|*[!0-9]*) MIN_TURNS=3 ;; esac
case "$TURN_WINDOW" in ''|*[!0-9]*) TURN_WINDOW=400 ;; esac
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

# Persist the recovery handle before starting any provider work. The job holds
# only bounded metadata and a transcript path/hash; the raw transcript remains
# in Claude's existing project store and is not copied or indexed here.
if [ -L "$PENDING_DIR" ]; then
  log "enqueue failed reason=pending-dir-symlink"
  exit 0
fi
mkdir -p "$PENDING_DIR" 2>/dev/null || {
  log "enqueue failed reason=pending-dir-create"
  exit 0
}
chmod 700 "$PENDING_DIR" 2>/dev/null || true

TRANSCRIPT_HASH="$(sha256sum "$TRANSCRIPT_PATH" 2>/dev/null | awk '{print $1}')"
[ -n "$TRANSCRIPT_HASH" ] || {
  log "enqueue failed reason=transcript-hash"
  exit 0
}
JOB_ID="$(printf '%s\0%s\0%s' "$SESSION_ID" "$TRANSCRIPT_HASH" "v1" | sha256sum | awk '{print $1}')"
PENDING_JOB="$PENDING_DIR/$JOB_ID.json"

if [ -L "$PENDING_JOB" ]; then
  log "enqueue failed reason=job-symlink job=$JOB_ID"
  exit 0
fi
if [ ! -f "$PENDING_JOB" ]; then
  JOB_TMP="$(mktemp "$PENDING_DIR/.job.XXXXXX")" || {
    log "enqueue failed reason=mktemp"
    exit 0
  }
  chmod 600 "$JOB_TMP" 2>/dev/null || true
  if ! jq -n \
    --arg schema "ccc.distill.pending.v1" \
    --arg job_id "$JOB_ID" \
    --arg transcript_sha256 "$TRANSCRIPT_HASH" \
    --arg session_id "$SESSION_ID" \
    --arg transcript_path "$TRANSCRIPT_PATH" \
    --arg source_cwd "$SOURCE_CWD" \
    --arg source_project "$PROJECT_ENC" \
    --arg trigger "$TRIGGER" \
    --arg created_at "$(ts)" \
    --arg isolation_profile "${CCC_NODE_ISOLATION_PROFILE:-fleet}" \
    --arg wiki_memory_enabled "${CCC_WIKI_MEMORY_ENABLED:-1}" \
    --arg memory_user_label "${CCC_MEMORY_USER_LABEL:-Seo Jin On / 서진원}" \
    --arg memory_assistant_label "${CCC_MEMORY_ASSISTANT_LABEL:-dungae, a Hermes Team2 worker}" \
    --argjson dryrun "$DRYRUN" \
    '{schema:$schema, job_id:$job_id, transcript_sha256:$transcript_sha256,
      session_id:$session_id, transcript_path:$transcript_path,
      source_cwd:$source_cwd, source_project:$source_project,
      trigger:$trigger, dryrun:$dryrun, created_at:$created_at,
      isolation_profile:$isolation_profile,
      wiki_memory_enabled:$wiki_memory_enabled,
      memory_user_label:$memory_user_label,
      memory_assistant_label:$memory_assistant_label}' > "$JOB_TMP"; then
    rm -f "$JOB_TMP" 2>/dev/null || true
    log "enqueue failed reason=serialize"
    exit 0
  fi
  command -v sync >/dev/null 2>&1 && sync -f "$JOB_TMP" 2>/dev/null || true
  mv "$JOB_TMP" "$PENDING_JOB" 2>/dev/null || {
    rm -f "$JOB_TMP" 2>/dev/null || true
    log "enqueue failed reason=atomic-move job=$JOB_ID"
    exit 0
  }
  command -v sync >/dev/null 2>&1 && sync -f "$PENDING_DIR" 2>/dev/null || true
  log "enqueued job=$JOB_ID trigger=$TRIGGER"
else
  log "enqueue dedup job=$JOB_ID trigger=$TRIGGER"
fi
export CLAUDE_DISTILL_JOB="$PENDING_JOB"

# Prefer `setsid`: a plain disowned subshell stays in the hook's process
# group/session, so when the parent session is torn down as a group (ssh-driven
# maintenance sessions, CLI teardown) the pipeline dies silently before logging
# anything — observed fleet-wide on 2026-07-07. The shared helper keeps the
# legacy subshell fallback for environments without setsid.
DISTILL_SELF="${BASH_SOURCE[0]:-$0}"
DISTILL_HOOKDIR="$(cd "$(dirname "$DISTILL_SELF")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/spawn-detached.sh
if [ -n "$DISTILL_HOOKDIR" ] \
  && [ -r "$DISTILL_HOOKDIR/lib/spawn-detached.sh" ]; then
  . "$DISTILL_HOOKDIR/lib/spawn-detached.sh"
  if spawn_detached "$DISTILL_SELF" CLAUDE_DISTILL_BG run_bg_pipeline "$TRIGGER"; then
    log "spawned bg pid=$SPAWN_DETACHED_PID mode=$SPAWN_DETACHED_MODE"
  else
    log "spawn failed reason=invalid-detached-contract"
  fi
else
  log "spawn failed reason=missing-detached-helper"
fi
exit 0
