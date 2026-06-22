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
STATE_DIR=/root/.claude/state
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

log "start trigger=$TRIGGER dryrun=$DRYRUN pid=$$"

# ---- read hook stdin payload (PreCompact/SessionEnd give JSON, manual = empty)
HOOK_INPUT="$(cat 2>/dev/null || true)"
SESSION_ID="$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$HOOK_INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

# Fallback: find the most-recent transcript jsonl for cwd-encoded project dir.
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  PROJ_ENC="$(printf '%s' "${PWD:-/root}" | sed 's|/|-|g')"
  TRANSCRIPT_PATH="$(ls -t "/root/.claude/projects/$PROJ_ENC"/*.jsonl 2>/dev/null | head -1)"
fi

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  log "skip reason=no-transcript"
  exit 0
fi

[ -z "$SESSION_ID" ] && SESSION_ID="$(basename "$TRANSCRIPT_PATH" .jsonl)"
log "transcript=$TRANSCRIPT_PATH session=$SESSION_ID"

# ---- min-content gate (skip trivial sessions) ------------------------------
LINES="$(wc -l < "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)"
if [ "$LINES" -lt 6 ]; then
  log "skip reason=too-few-lines lines=$LINES"
  exit 0
fi

# ---- fire pipeline (detach so hook returns fast; SessionEnd has tight timeout)
# Dynamic hookdir — works in both standalone (~/.claude/hooks/) and plugin
# (${CLAUDE_PLUGIN_ROOT}/hooks/) modes. distill/ sub-scripts must sit next to this file.
HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HOOKDIR=/root/.claude/hooks
(
  export CLAUDE_DISTILL_INFLIGHT=1
  export CLAUDE_DISTILL_TRIGGER="$TRIGGER"
  export CLAUDE_DISTILL_SESSION="$SESSION_ID"
  export CLAUDE_DISTILL_TRANSCRIPT="$TRANSCRIPT_PATH"
  export CLAUDE_DISTILL_DRYRUN="$DRYRUN"

  EXTRACT_OUT="$(bash "$HOOKDIR/distill/extract.sh" 2>>"$LOG")"
  ec=$?
  if [ $ec -ne 0 ] || [ -z "$EXTRACT_OUT" ]; then
    log "extract failed ec=$ec"
    exit 0
  fi

  # Stash extracted JSON for debugging + sub-script consumption.
  STASH="$STATE_DIR/distill-last.json"
  printf '%s' "$EXTRACT_OUT" > "$STASH" 2>/dev/null

  if [ "$DRYRUN" = "1" ]; then
    log "dry-run skipping honcho/wiki push (see $STASH)"
    exit 0
  fi

  bash "$HOOKDIR/distill/honcho-push.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "honcho-push non-zero (queued for retry)"
  bash "$HOOKDIR/distill/wiki-queue.sh" < "$STASH" >> "$LOG" 2>&1 || \
    log "wiki-queue non-zero"

  log "done"
) </dev/null >/dev/null 2>&1 &
BG_PID=$!
disown 2>/dev/null || true
log "spawned bg pid=$BG_PID"
exit 0
