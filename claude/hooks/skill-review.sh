#!/usr/bin/env bash
# Skill Review — Hermes-style self-improvement pass for ccc-node.
# Fired by SessionEnd / manual command. It reviews the recent Claude Code
# transcript and stages reusable skill drafts under ~/.claude/state/pending-skills/.
# This script never writes ~/.claude/skills itself. In approve mode (default)
# drafts wait for human approval; in auto mode (#355, CCC_SKILL_AUTOSAVE_MODE=auto)
# staged drafts are handed to skill-review/autoinstall.sh, whose machine gates
# install passing drafts and leave failing ones pending for humans.
#
# Safety:
#   - Always exit 0 when used as a hook.
#   - Recursion guard prevents child `claude -p` sessions from re-firing hooks.
#   - Redaction happens in skill-review/extract.sh before model input.
#   - Off-switch: touch ~/.claude/state/skill-review.disabled
#   - Cooldown: CCC_SKILL_REVIEW_COOLDOWN_SECONDS (default 3600) for hook triggers.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/skill-review.log"
PENDING_DIR="$STATE_DIR/pending-skills"
mkdir -p "$STATE_DIR" "$PENDING_DIR" 2>/dev/null

TRIGGER="${1:-manual}"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG" 2>/dev/null; }

run_skill_review_bg() {
  cd "${HOME:-/root}" 2>/dev/null || cd / 2>/dev/null || true
  export CLAUDE_SKILL_REVIEW_INFLIGHT=1
  # Reuse the recursion guard understood by the memory/distill hook family.
  export CLAUDE_DISTILL_INFLIGHT=1

  local TRIGGER="${CLAUDE_SKILL_REVIEW_TRIGGER:-manual}"
  local SESSION_ID="${CLAUDE_SKILL_REVIEW_SESSION:-unknown}"
  local TRANSCRIPT_PATH="${CLAUDE_SKILL_REVIEW_TRANSCRIPT:-}"
  local SOURCE_CWD="${CLAUDE_SKILL_REVIEW_SOURCE_CWD:-}"
  local PROJECT_ENC="${CLAUDE_SKILL_REVIEW_SOURCE_PROJECT:-}"
  local PENDING_DIR="${CCC_SKILL_REVIEW_PENDING_DIR:-$STATE_DIR/pending-skills}"
  local HOOKDIR
  HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HOOKDIR=${HOME:-/root}/.claude/hooks
  export CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-${HOME:-/root}/.claude/skills}"
  mkdir -p "$PENDING_DIR" 2>/dev/null

  local PIPE_PID OUT ec STASH count i staged item name skill_md id safe_id dest
  PIPE_PID="${BASHPID:-$$}"
  OUT="$(bash "$HOOKDIR/skill-review/extract.sh" 2>>"$LOG")"
  ec=$?
  if [ $ec -ne 0 ] || [ -z "$OUT" ]; then
    log "extract failed ec=$ec trigger=$TRIGGER pid=$PIPE_PID"
    return 0
  fi

  STASH="$STATE_DIR/skill-review-last.json"
  printf '%s' "$OUT" > "$STASH" 2>/dev/null || true

  count="$(printf '%s' "$OUT" | jq '.skill_candidates | length' 2>/dev/null || printf 0)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  if [ "$count" -eq 0 ]; then
    log "done staged=0 trigger=$TRIGGER pid=$PIPE_PID"
    return 0
  fi

  i=0
  staged=0
  while [ "$i" -lt "$count" ]; do
    item="$(printf '%s' "$OUT" | jq -c --argjson i "$i" '.skill_candidates[$i]' 2>/dev/null)"
    name="$(printf '%s' "$item" | jq -r '.name // empty' 2>/dev/null \
      | tr '[:upper:]' '[:lower:]' \
      | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g' \
      | cut -c1-64)"
    [ -z "$name" ] && name="candidate-$i"
    skill_md="$(printf '%s' "$item" | jq -r '.skill_md // empty' 2>/dev/null)"
    if [ -z "$skill_md" ] || ! printf '%s' "$skill_md" | grep -q '^---'; then
      log "candidate skipped reason=invalid-skill-md name=$name trigger=$TRIGGER pid=$PIPE_PID"
      i=$((i + 1)); continue
    fi
    id="$(date -u +%Y%m%d-%H%M%S)-${SESSION_ID}-${name}"
    safe_id="$(printf '%s' "$id" | sed -E 's/[^A-Za-z0-9._-]+/-/g' | cut -c1-160)"
    dest="$PENDING_DIR/$safe_id"
    mkdir -p "$dest" 2>/dev/null || { i=$((i + 1)); continue; }
    printf '%s\n' "$skill_md" > "$dest/SKILL.md"
    printf '%s' "$item" | jq -c \
      --arg id "$safe_id" \
      --arg session "$SESSION_ID" \
      --arg trigger "$TRIGGER" \
      --arg source_cwd "$SOURCE_CWD" \
      --arg source_project "$PROJECT_ENC" \
      --arg staged_at "$(ts)" \
      --arg skill_path "${CLAUDE_SKILLS_DIR%/}/$name/SKILL.md" \
      'del(.skill_md) + {id:$id,status:"pending",session_id:$session,trigger:$trigger,source_cwd:$source_cwd,source_project:$source_project,staged_at:$staged_at,target_skill_path:$skill_path}' \
      > "$dest/meta.json" 2>/dev/null || true
    jq -c --arg id "$safe_id" --arg name "$name" --arg session "$SESSION_ID" --arg trigger "$TRIGGER" --arg at "$(ts)" \
      '{id:$id,name:$name,status:"pending",session_id:$session,trigger:$trigger,staged_at:$at}' \
      >> "$PENDING_DIR/index.jsonl" 2>/dev/null || true
    staged=$((staged + 1))
    log "staged id=$safe_id name=$name trigger=$TRIGGER pid=$PIPE_PID"
    i=$((i + 1))
  done

  if [ "$staged" -gt 0 ]; then
    # Auto mode (#355): hand fresh drafts to the machine-gated installer right
    # away (Hermes-style — no human gate). The script is a strict no-op unless
    # CCC_SKILL_AUTOSAVE_MODE=auto (or the skill-autosave.mode state file says
    # auto), so approve-mode nodes are unchanged.
    local AUTOINSTALL summary still_pending
    AUTOINSTALL="${CCC_SKILL_AUTOINSTALL_CMD:-$HOOKDIR/skill-review/autoinstall.sh}"
    if [ -f "$AUTOINSTALL" ]; then
      summary="$(CCC_SKILL_AUTOSAVE_TRIGGER="hook-$TRIGGER" bash "$AUTOINSTALL" run 2>>"$LOG")" \
        && log "autoinstall $(printf '%s' "$summary" | head -c 500)" \
        || log "autoinstall failed (non-fatal)"
    fi
    # Approval marker only for drafts that still wait for a human — in auto
    # mode an installed draft needs review/rollback, not approval.
    still_pending="$(find "$PENDING_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
      | grep -cEv '\.(approved|rejected|installed)-[0-9]+$')"
    case "$still_pending" in ''|*[!0-9]*) still_pending="$staged" ;; esac
    if [ "$still_pending" -gt 0 ]; then
      printf '%s\t%s\n' "$(ts)" "PENDING_SKILL_REVIEW staged=$staged pending=$still_pending session=$SESSION_ID" \
        >> "$STATE_DIR/approval-needed.log" 2>/dev/null || true
    fi
  fi
  log "done staged=$staged trigger=$TRIGGER pid=$PIPE_PID"
  return 0
}

# A setsid child re-enters here before transcript discovery/cooldown. All of its
# inputs were explicitly exported by the parent spawn site below.
if [ "${CLAUDE_SKILL_REVIEW_BG:-}" = "1" ]; then
  run_skill_review_bg
  exit 0
fi

# Distill subprocesses and nested skill-review subprocesses should not recurse.
if [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] || [ -n "${CLAUDE_SKILL_REVIEW_INFLIGHT:-}" ]; then
  exit 0
fi

if [ -f "$STATE_DIR/skill-review.disabled" ]; then
  log "skip reason=disabled trigger=$TRIGGER pid=$$"
  exit 0
fi

encode_project_dir() { printf '%s' "$1" | sed -E 's|[^A-Za-z0-9_]|-|g'; }
legacy_project_dir() { printf '%s' "$1" | sed 's|/|-|g'; }

HOOK_INPUT="$(cat 2>/dev/null || true)"
SESSION_ID="$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)"
TRANSCRIPT_PATH="$(printf '%s' "$HOOK_INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"
SOURCE_CWD="$(printf '%s' "$HOOK_INPUT" | jq -r '.cwd // .workspace.current_dir // .workspace.cwd // empty' 2>/dev/null)"

PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-${HOME:-/root}/.claude/projects}"
if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  for PROJ_ENC in "$(encode_project_dir "${PWD:-/root}")" "$(legacy_project_dir "${PWD:-/root}")"; do
    TRANSCRIPT_PATH="$(ls -t "$PROJECTS_DIR/$PROJ_ENC"/*.jsonl 2>/dev/null | head -1)"
    [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] && break
  done
fi

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  log "skip reason=no-transcript trigger=$TRIGGER pid=$$"
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

# Min-content gate: skip small sessions and sessions without enough assistant/user material.
MIN_TURNS="${CCC_SKILL_REVIEW_MIN_TURNS:-4}"
TURN_WINDOW="${CCC_SKILL_REVIEW_TURN_WINDOW:-500}"
case "$MIN_TURNS" in ''|*[!0-9]*) MIN_TURNS=4 ;; esac
case "$TURN_WINDOW" in ''|*[!0-9]*) TURN_WINDOW=500 ;; esac
TURNS="$(tail -n "$TURN_WINDOW" "$TRANSCRIPT_PATH" 2>/dev/null \
  | jq -r 'select(.type == "user" or .type == "assistant") | .type' 2>/dev/null \
  | wc -l | tr -d '[:space:]')"
case "$TURNS" in ''|*[!0-9]*) TURNS=0 ;; esac
if [ "$TURNS" -lt "$MIN_TURNS" ]; then
  log "skip reason=too-few-turns turns=$TURNS min_turns=$MIN_TURNS trigger=$TRIGGER pid=$$"
  exit 0
fi

# Hook triggers are cost-bearing. Manual runs bypass cooldown.
COOLDOWN="${CCC_SKILL_REVIEW_COOLDOWN_SECONDS:-3600}"
case "$COOLDOWN" in ''|*[!0-9]*) COOLDOWN=3600 ;; esac
LAST_FILE="$STATE_DIR/skill-review.last"
if [ "$TRIGGER" != "manual" ] && [ "$COOLDOWN" -gt 0 ] && [ -f "$LAST_FILE" ]; then
  now="$(date -u +%s)"
  last="$(cat "$LAST_FILE" 2>/dev/null || printf 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  age=$((now - last))
  if [ "$age" -lt "$COOLDOWN" ]; then
    log "skip reason=cooldown age_s=$age cooldown_s=$COOLDOWN trigger=$TRIGGER pid=$$"
    exit 0
  fi
fi

date -u +%s > "$LAST_FILE" 2>/dev/null || true
log "start trigger=$TRIGGER session=$SESSION_ID transcript=$TRANSCRIPT_PATH source_cwd=$SOURCE_CWD project=$PROJECT_ENC turns=$TURNS pid=$$"

HOOKDIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HOOKDIR=${HOME:-/root}/.claude/hooks
export CLAUDE_SKILL_REVIEW_TRIGGER="$TRIGGER"
export CLAUDE_SKILL_REVIEW_SESSION="$SESSION_ID"
export CLAUDE_SKILL_REVIEW_TRANSCRIPT="$TRANSCRIPT_PATH"
export CLAUDE_SKILL_REVIEW_SOURCE_CWD="$SOURCE_CWD"
export CLAUDE_SKILL_REVIEW_SOURCE_PROJECT="$PROJECT_ENC"
export CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-${HOME:-/root}/.claude/skills}"
export CCC_SKILL_REVIEW_PENDING_DIR="$PENDING_DIR"

SKILL_REVIEW_SELF="${BASH_SOURCE[0]:-$0}"
# shellcheck source=claude/hooks/lib/spawn-detached.sh
if [ -r "$HOOKDIR/lib/spawn-detached.sh" ]; then
  . "$HOOKDIR/lib/spawn-detached.sh"
  if spawn_detached "$SKILL_REVIEW_SELF" CLAUDE_SKILL_REVIEW_BG run_skill_review_bg "$TRIGGER"; then
    log "spawned bg pid=$SPAWN_DETACHED_PID mode=$SPAWN_DETACHED_MODE"
  else
    log "spawn failed reason=invalid-detached-contract"
  fi
else
  log "spawn failed reason=missing-detached-helper"
fi
exit 0
