#!/usr/bin/env bash
# ccc-skill-autosave — Hermes-style "auto-skillification" sweep for ccc-node.
#
# The SessionEnd hook wiring (skill-review.sh) only covers interactive `claude`
# sessions: Telegram-bridge / SDK sessions never fire hooks, and their
# persistent streams rarely "end" at all — yet their transcripts land in the
# same ~/.claude/projects/*.jsonl tree. This sweep closes that gap: run it from
# cron (see install-skill-autosave-cron.sh) and it
#   1. refreshes the deterministic skill-candidate report (skill-suggest/scan.sh),
#   2. pushes recent, not-yet-reviewed transcripts (bridge sessions included)
#      through the existing skill-review.sh drafting pipeline, and
#   3. queues an owner-only Telegram notification when skill drafts are waiting
#      for approval (delivered by the bridge PushNotifier — token never touched).
#
# Safety (same contract as the hooks it orchestrates):
#   - Always exits 0; every step is best-effort and logged.
#   - approve mode (default): never installs or overwrites ~/.claude/skills —
#     drafts stay in the human-gated pending-skills queue (/skill-suggest).
#   - auto mode (#355, opt-in via CCC_SKILL_AUTOSAVE_MODE=auto or `auto` in
#     ~/.claude/state/skill-autosave.mode): after drafting, the machine-gated
#     installer (hooks/skill-review/autoinstall.sh) installs passing drafts and
#     queues a post-hoc Telegram notice; gate failures stay pending for humans.
#   - Off-switch: touch ~/.claude/state/skill-autosave.disabled
#     (skill-review's own skill-review.disabled off-switch is honored too).
#   - Cost-bounded: at most CCC_SKILL_AUTOSAVE_MAX_SESSIONS transcripts are
#     drafted per run; a ledger prevents re-drafting a transcript that has not
#     grown since it was last processed.
set -uo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
PROJECTS_DIR="${CLAUDE_PROJECTS_DIR:-$CLAUDE_DIR/projects}"
PENDING_DIR="$STATE_DIR/pending-skills"
LOG="$STATE_DIR/skill-autosave.log"
LEDGER="$STATE_DIR/skill-autosave.seen"
NOTIFIED="$STATE_DIR/skill-autosave.notified"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"

REVIEW="${CCC_SKILL_REVIEW_CMD:-$CLAUDE_DIR/hooks/skill-review.sh}"
SCAN="${CCC_SKILL_SCAN_CMD:-$CLAUDE_DIR/skills/skill-suggest/scan.sh}"
AUTOINSTALL="${CCC_SKILL_AUTOINSTALL_CMD:-$CLAUDE_DIR/hooks/skill-review/autoinstall.sh}"

# Fleet-wide autonomy guard (#386): a single kill-switch/dry-run above every
# no-approval write. The installed layout keeps the lib under the claude tree;
# the repo checkout keeps it beside this script's ../claude. Sourced fail-open —
# a missing lib leaves ccc_autonomy_state undefined and the sweep runs as today.
AUTOSAVE_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
for _autonomy_lib in \
  "$CLAUDE_DIR/hooks/lib/autonomy-guard.sh" \
  "$AUTOSAVE_SELF_DIR/../claude/hooks/lib/autonomy-guard.sh"; do
  if [ -f "$_autonomy_lib" ]; then
    # shellcheck source=claude/hooks/lib/autonomy-guard.sh
    . "$_autonomy_lib" 2>/dev/null || true
    break
  fi
done
unset _autonomy_lib

MAX_SESSIONS="${CCC_SKILL_AUTOSAVE_MAX_SESSIONS:-3}"
WINDOW_DAYS="${CCC_SKILL_AUTOSAVE_WINDOW_DAYS:-2}"
# A processed transcript becomes eligible again only after growing by this many
# bytes (persistent bridge streams keep appending to one jsonl for days).
REGROWTH_BYTES="${CCC_SKILL_AUTOSAVE_REGROWTH_BYTES:-16384}"
NOTIFY="${CCC_SKILL_AUTOSAVE_NOTIFY:-1}"
case "$MAX_SESSIONS" in ''|*[!0-9]*) MAX_SESSIONS=3 ;; esac
case "$WINDOW_DAYS" in ''|*[!0-9]*) WINDOW_DAYS=2 ;; esac
case "$REGROWTH_BYTES" in ''|*[!0-9]*) REGROWTH_BYTES=16384 ;; esac

mkdir -p "$STATE_DIR" "$PENDING_DIR" 2>/dev/null
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG" 2>/dev/null; }

pending_count() {
  find "$PENDING_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
    | grep -Ev '\.(approved|rejected|installed)-[0-9]+$' | wc -l | tr -d '[:space:]'
}

# approve (default) keeps the human gate; auto (#355) hands passing drafts to
# the machine-gated installer. Env wins over the state file.
resolve_mode() {
  local m="${CCC_SKILL_AUTOSAVE_MODE:-}"
  if [ -z "$m" ] && [ -f "$STATE_DIR/skill-autosave.mode" ]; then
    m="$(head -1 "$STATE_DIR/skill-autosave.mode" 2>/dev/null | tr -d '[:space:]')"
  fi
  case "$m" in auto) printf 'auto' ;; *) printf 'approve' ;; esac
}

MODE="${1:-run}"

if [ "$MODE" = "status" ]; then
  echo "mode: $(resolve_mode) (approve = human gate, auto = machine gate + post-hoc notify)"
  echo "off-switch: $([ -f "$STATE_DIR/skill-autosave.disabled" ] && echo ON || echo off)"
  echo "autonomy: $(declare -f ccc_autonomy_state >/dev/null 2>&1 && ccc_autonomy_state || echo active) (kill = skip whole sweep, dry-run = draft/report only)"
  echo "pending skill drafts: $(pending_count)"
  echo "candidates report: $(ls -la "$STATE_DIR/skill-candidates.md" 2>/dev/null || echo none)"
  echo "-- ledger (last 5) --";      tail -5 "$LEDGER" 2>/dev/null
  echo "-- autosave installs (last 5) --"; tail -5 "$STATE_DIR/skill-autosave-install.jsonl" 2>/dev/null
  echo "-- log (last 10) --";        tail -10 "$LOG" 2>/dev/null
  exit 0
fi

if [ "$MODE" != "run" ]; then
  echo "usage: ccc-skill-autosave.sh [run|status]" >&2
  exit 0
fi

if [ -f "$STATE_DIR/skill-autosave.disabled" ]; then
  log "skip reason=disabled pid=$$"
  exit 0
fi

# Fleet-wide autonomy guard (#386). kill halts the whole sweep: no drafting LLM
# call, no pending-draft staging, no notify — nothing this sweep does is an
# approved write. dry-run/active proceed here; the install layer (autoinstall)
# self-guards, so under dry-run drafts still stage for human review but nothing
# auto-installs. Fail-open: undefined guard (missing lib) => active.
AUTONOMY_STATE="active"
if declare -f ccc_autonomy_state >/dev/null 2>&1; then
  AUTONOMY_STATE="$(ccc_autonomy_state 2>/dev/null || echo active)"
fi
if [ "$AUTONOMY_STATE" = "kill" ]; then
  log "skip reason=autonomy-kill pid=$$"
  declare -f ccc_autonomy_record >/dev/null 2>&1 \
    && CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_record skill-autosave kill sweep
  exit 0
fi

# --- 1) refresh the deterministic candidate report (best-effort) -------------
if [ -f "$SCAN" ]; then
  if bash "$SCAN" >/dev/null 2>>"$LOG"; then
    log "scan ok out=$STATE_DIR/skill-candidates.md"
  else
    log "scan failed (non-fatal)"
  fi
else
  log "scan skipped reason=no-scanner path=$SCAN"
fi

# --- 2) draft skills from recent, unprocessed transcripts --------------------
drafted=0
if [ ! -f "$REVIEW" ]; then
  log "review skipped reason=no-skill-review path=$REVIEW"
elif [ -f "$STATE_DIR/skill-review.disabled" ]; then
  log "review skipped reason=skill-review-disabled"
else
  touch "$LEDGER" 2>/dev/null
  before="$(pending_count)"
  while IFS= read -r transcript; do
    [ "$drafted" -ge "$MAX_SESSIONS" ] && break
    [ -f "$transcript" ] || continue
    sid="$(basename "$transcript" .jsonl)"
    size="$(wc -c < "$transcript" 2>/dev/null | tr -d '[:space:]')"
    case "$size" in ''|*[!0-9]*) size=0 ;; esac
    last_size="$(awk -F'\t' -v s="$sid" '$1==s {sz=$3} END {print sz+0}' "$LEDGER" 2>/dev/null)"
    case "$last_size" in ''|*[!0-9]*) last_size=0 ;; esac
    if [ "$last_size" -gt 0 ] && [ $((size - last_size)) -lt "$REGROWTH_BYTES" ]; then
      continue
    fi
    # skill-review.sh derives cwd/project from the transcript path itself; the
    # "manual" trigger bypasses its hook cooldown (this sweep budgets itself).
    if jq -nc --arg sid "$sid" --arg tp "$transcript" \
        '{session_id:$sid, transcript_path:$tp}' 2>/dev/null \
        | bash "$REVIEW" manual >>"$LOG" 2>&1; then
      drafted=$((drafted + 1))
      tmp="$LEDGER.tmp.$$"
      { awk -F'\t' -v s="$sid" '$1!=s' "$LEDGER" 2>/dev/null;
        printf '%s\t%s\t%s\n' "$sid" "$(ts)" "$size"; } > "$tmp" 2>/dev/null \
        && mv "$tmp" "$LEDGER" 2>/dev/null
      log "review ok session=$sid size=$size"
    else
      log "review failed session=$sid (non-fatal)"
    fi
  done < <(find "$PROJECTS_DIR" -name '*.jsonl' -type f -mtime -"$WINDOW_DAYS" 2>/dev/null \
             | xargs -r ls -t 2>/dev/null)

  # skill-review.sh stages drafts from a detached background pipeline; give it
  # a bounded window to settle so this run's notification (step 3) can already
  # count fresh drafts. A quiet pipeline (no reusable procedure found) simply
  # times out and the next scheduled run picks up whatever landed later.
  SETTLE="${CCC_SKILL_AUTOSAVE_SETTLE_SECONDS:-90}"
  case "$SETTLE" in ''|*[!0-9]*) SETTLE=90 ;; esac
  if [ "$drafted" -gt 0 ] && [ "$SETTLE" -gt 0 ]; then
    waited=0
    while [ "$waited" -lt "$SETTLE" ]; do
      [ "$(pending_count)" != "$before" ] && break
      sleep 5; waited=$((waited + 5))
    done
  fi
  after="$(pending_count)"
  log "sweep done drafted_sessions=$drafted pending_before=$before pending_after=$after"
fi

# --- 2b) auto mode (#355): machine-gate + install passing drafts -------------
# autoinstall.sh no-ops unless mode=auto; it owns the gates, the daily cap, the
# installed-by=autosave ledger and the post-hoc Telegram notice for installs
# and blocks, so the sweep just invokes it and records the summary.
EFFECTIVE_MODE="$(resolve_mode)"
if [ "$EFFECTIVE_MODE" = "auto" ]; then
  if [ -f "$AUTOINSTALL" ]; then
    summary="$(CCC_SKILL_AUTOSAVE_TRIGGER=sweep bash "$AUTOINSTALL" run 2>>"$LOG")" \
      && log "autoinstall $(printf '%s' "$summary" | head -c 500)" \
      || log "autoinstall failed (non-fatal)"
  else
    # Fall back to the approve-mode reminder below rather than going silent.
    EFFECTIVE_MODE="approve"
    log "autoinstall skipped reason=missing path=$AUTOINSTALL"
  fi
fi

# --- 3) owner-only Telegram notification via the bridge spool ----------------
# Same token-isolation contract as notify.sh: this script never touches the bot
# token; it writes a short summary file that the bridge PushNotifier (opt-in,
# CCC_PUSH_ENABLED) delivers to the owner chat. In auto mode this approval
# reminder is replaced by autoinstall's own post-hoc install/block notice.
pending="$(pending_count)"
last_notified="$(cat "$NOTIFIED" 2>/dev/null || printf 0)"
case "$last_notified" in ''|*[!0-9]*) last_notified=0 ;; esac
if [ "$EFFECTIVE_MODE" = "approve" ] && [ "$NOTIFY" = "1" ] && [ "$pending" -gt 0 ] && [ "$pending" != "$last_notified" ]; then
  if mkdir -p "$SPOOL" 2>/dev/null; then
    node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
    text="스킬 초안 ${pending}건 승인 대기 중 — '/skill-suggest'로 검토/승인하세요."
    now="$(ts)"
    fname="$SPOOL/$(printf '%s' "$now" | tr ':' '-')-SkillAutosave-$$.json"
    if jq -nc --arg ts "$now" --arg node "$node" --arg text "$text" --arg n "$pending" \
        '{ts:$ts, event:"SkillAutosave", node:$node, text:$text,
          dedup:("SkillAutosave:"+$n)}' > "$fname" 2>/dev/null; then
      printf '%s\n' "$pending" > "$NOTIFIED" 2>/dev/null
      log "notify queued pending=$pending spool=$fname"
    else
      rm -f "$fname" 2>/dev/null
      log "notify failed (non-fatal)"
    fi
  fi
fi

exit 0
