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

# ---- recursion guard (FIRST line of executable logic) ----------------------
if [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ]; then
  exit 0
fi

# ---- off-switch ------------------------------------------------------------
# State dir is overridable for testing / non-root installs (#73).
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
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
# Dynamic hookdir — works in both standalone (~/.claude/hooks/) and plugin
# (${CLAUDE_PLUGIN_ROOT}/hooks/) modes. distill/ sub-scripts must sit next to this file.
HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HOOKDIR=${HOME:-/root}/.claude/hooks
(
  export CLAUDE_DISTILL_INFLIGHT=1
  export CLAUDE_DISTILL_TRIGGER="$TRIGGER"
  export CLAUDE_DISTILL_SESSION="$SESSION_ID"
  export CLAUDE_DISTILL_TRANSCRIPT="$TRANSCRIPT_PATH"
  export CLAUDE_DISTILL_SOURCE_CWD="$SOURCE_CWD"
  export CLAUDE_DISTILL_SOURCE_PROJECT="$PROJECT_ENC"
  export CLAUDE_DISTILL_DRYRUN="$DRYRUN"

  PIPE_START_EPOCH="$(date -u +%s)"
  PIPE_PID="${BASHPID:-$$}"
  elapsed_s() { now="$(date -u +%s)"; printf '%s' "$((now - PIPE_START_EPOCH))"; }

  EXTRACT_OUT="$(bash "$HOOKDIR/distill/extract.sh" 2>>"$LOG")"
  ec=$?
  if [ $ec -ne 0 ] || [ -z "$EXTRACT_OUT" ]; then
    log "extract failed ec=$ec trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
    exit 0
  fi

  # Stash extracted JSON for debugging + sub-script consumption.
  STASH="$STATE_DIR/distill-last.json"
  STASH_DIR="$STATE_DIR/distill-history"
  HISTORY_KEEP="${CCC_DISTILL_HISTORY_KEEP:-20}"
  case "$HISTORY_KEEP" in ''|*[!0-9]*) HISTORY_KEEP=20 ;; esac
  if [ -f "$STASH" ]; then
    mkdir -p "$STASH_DIR" 2>/dev/null
    cp -p "$STASH" "$STASH_DIR/$(date -u +%Y%m%d-%H%M%S)-${BASHPID:-$$}.json" 2>/dev/null || true
  fi
  printf '%s' "$EXTRACT_OUT" > "$STASH" 2>/dev/null
  if [ "$HISTORY_KEEP" -gt 0 ]; then
    find "$STASH_DIR" -maxdepth 1 -type f -name '*.json' -printf '%T@ %p\n' 2>/dev/null \
      | sort -rn \
      | awk -v keep="$HISTORY_KEEP" 'NR > keep { sub(/^[^ ]+ /, ""); print }' \
      | xargs -r rm -- 2>/dev/null || true
  fi

  if [ "$DRYRUN" = "1" ]; then
    log "dry-run skipping honcho/wiki push (see $STASH) trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
    exit 0
  fi

  bash "$HOOKDIR/distill/honcho-push.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "honcho-push non-zero (queued for retry)"
  bash "$HOOKDIR/distill/wiki-queue.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "wiki-queue non-zero"
  bash "$HOOKDIR/distill/local-facts.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "local-facts non-zero"

  log "done trigger=$TRIGGER pid=$PIPE_PID elapsed_s=$(elapsed_s)"
) </dev/null >/dev/null 2>&1 &
BG_PID=$!
disown 2>/dev/null || true
log "spawned bg pid=$BG_PID"
exit 0
