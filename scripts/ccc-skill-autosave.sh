#!/usr/bin/env bash
# ccc-skill-autosave — Hermes-style "auto-skillification" sweep for ccc-node.
#
# The SessionEnd hook wiring (skill-review.sh) only covers interactive `claude`
# sessions: Telegram-bridge / SDK sessions never fire hooks, and their
# persistent streams rarely "end" at all — yet their transcripts land in the
# same ~/.claude/projects/*.jsonl tree. This sweep closes that gap: run it from
# cron (see install-skill-autosave-cron.sh) and it
#   1. refreshes the deterministic skill-candidate report (skillsuggest/scan.sh),
#   2. pushes recent, not-yet-reviewed transcripts (bridge sessions included)
#      through the existing skill-review.sh drafting pipeline, and
#   3. queues an owner-only Telegram notification when skill drafts are waiting
#      for approval (delivered by the bridge PushNotifier — token never touched).
#
# Safety (same contract as the hooks it orchestrates):
#   - Always exits 0; every step is best-effort and logged.
#   - approve mode (default; `review` is an accepted alias, #2011): never
#     installs or overwrites ~/.claude/skills — drafts stay in the human-gated
#     pending-skills queue (/skillsuggest) until an operator reviews them.
#   - auto mode (#355, opt-in via CCC_SKILL_AUTOSAVE_MODE=auto or `auto` in
#     ~/.claude/state/skill-autosave.mode): after drafting, the machine-gated
#     installer (hooks/skill-review/autoinstall.sh) installs passing drafts and
#     queues a post-hoc Telegram notice; gate failures stay pending for humans.
#     No longer recommended (#2011): `set-mode review` moves a node back to the
#     human gate — explicitly, never silently.
#   - Off-switch: touch ~/.claude/state/skill-autosave.disabled
#     (skill-review's own skill-review.disabled off-switch is honored too).
#   - Cost-bounded: at most CCC_SKILL_AUTOSAVE_MAX_SESSIONS transcripts are
#     drafted per run PER BRANCH (claude/codex/piri/danso each have their own
#     counter, #1824); the optional CCC_SKILL_AUTOSAVE_TOTAL_MAX_SESSIONS caps
#     the sum across branches. A ledger prevents re-drafting a transcript that
#     has not grown since it was last processed.
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
SCAN="${CCC_SKILL_SCAN_CMD:-$CLAUDE_DIR/skills/skillsuggest/scan.sh}"
AUTOINSTALL="${CCC_SKILL_AUTOINSTALL_CMD:-$CLAUDE_DIR/hooks/skill-review/autoinstall.sh}"
CURATOR="${CCC_SKILL_CURATOR_CMD:-$CLAUDE_DIR/hooks/skill-review/curator.py}"
PROMOTER="${CCC_SKILL_PROMOTION_CMD:-$CLAUDE_DIR/hooks/ccc-skill-promotion.py}"
# Publisher edge env (#1766): the env file that exports A2A_EDGE_SECRET for the
# intake review dispatch in block 2d. HOME-relative default — no node-specific
# absolute path lives in this repo — and only the publisher actually has one.
EDGE_ENV="${CCC_A2A_EDGE_ENV:-${HOME:-/root}/.a2a-broker-edge.env}"

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
# #1824: MAX_SESSIONS is a per-branch budget, so N enabled branches can draft
# up to N x MAX_SESSIONS per run. This optional cross-branch cap (0/unset = no
# cap, the historical behavior) bounds the sum; branches run in the fixed
# order claude, codex, piri, danso and each stops once the sum reaches it.
TOTAL_MAX_SESSIONS="${CCC_SKILL_AUTOSAVE_TOTAL_MAX_SESSIONS:-0}"
case "$TOTAL_MAX_SESSIONS" in ''|*[!0-9]*) TOTAL_MAX_SESSIONS=0 ;; esac
case "$WINDOW_DAYS" in ''|*[!0-9]*) WINDOW_DAYS=2 ;; esac
case "$REGROWTH_BYTES" in ''|*[!0-9]*) REGROWTH_BYTES=16384 ;; esac
# #1932: skill-review.sh returns this code (CCC_SKILL_REVIEW_SKIP_RC) when it
# skips a transcript as not reviewable (too few turns). Such a skip is ledgered
# so it is not revisited, but it never counts against MAX_SESSIONS: one-turn
# `claude -p` batch transcripts (memory QA, the drafting call itself) are the
# newest files every evening and used to fill the whole budget silently.
REVIEW_SKIP_RC=3

mkdir -p "$STATE_DIR" "$PENDING_DIR" 2>/dev/null
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" 2>/dev/null >> "$LOG" || :; }

pending_count() {
  find "$PENDING_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
    | grep -Ev '\.(approved|rejected|installed)-[0-9]+$' | wc -l | tr -d '[:space:]'
}

MODE_FILE="$STATE_DIR/skill-autosave.mode"

# approve (default; `review` is its alias, #2011) keeps the human gate; auto
# (#355) hands passing drafts to the machine-gated installer. Env wins over the
# state file. Anything that is not exactly `auto` is the human gate, so an
# unset, empty or unknown value fails safe (autoinstall.sh resolves the same).
resolve_mode() {
  local m="${CCC_SKILL_AUTOSAVE_MODE:-}"
  if [ -z "$m" ] && [ -f "$MODE_FILE" ]; then
    m="$(head -1 "$MODE_FILE" 2>/dev/null | tr -d '[:space:]')"
  fi
  case "$m" in auto) printf 'auto' ;; *) printf 'approve' ;; esac
}

# Where the effective mode came from — env, state file, or the built-in default.
mode_source() {
  if [ -n "${CCC_SKILL_AUTOSAVE_MODE:-}" ]; then printf 'env'
  elif [ -f "$MODE_FILE" ]; then printf 'state-file'
  else printf 'default'
  fi
}

# #2011: auto installs LLM-drafted skills with no human review before install.
# The owner moved the fleet default to review, but switching an existing node
# stays an explicit operator action (never silently flipped by an update) — so
# auto nodes get a visible, actionable advisory instead.
AUTO_ADVISORY="auto mode installs LLM-drafted skills without pre-install human review; the fleet default is review (#2011). Migrate explicitly: ccc-skill-autosave.sh set-mode review"

# Run the central promoter with the publisher edge env loaded (#1766). Sourced
# inside a subshell so A2A_EDGE_SECRET reaches exactly the child that dispatches
# the intake review round and nothing else — not this script's environment, not
# any other step, never a log line. The file's own output is discarded and a
# malformed file is swallowed: a broken env file must not fail the sweep, and
# nothing it prints may reach the log. `set -a` covers both `KEY=value` and
# `export KEY=value` files; `set +u` keeps one that reads an unset variable from
# aborting the subshell under this script's `set -u`.
collect_with_edge_env() {
  (
    if [ -f "$EDGE_ENV" ]; then
      set +u
      set -a
      # shellcheck disable=SC1090
      . "$EDGE_ENV" >/dev/null 2>&1 || :
      set +a
      set -u
    fi
    python3 "$PROMOTER" "$@" 2>>"$LOG"
  )
}

# Curator switches (#2011). The curator itself validates the exact values and
# fails closed on garbage; the sweep only needs the off-switch direction.
curator_enabled() {
  case "$(printf '%s' "${CCC_SKILL_CURATOR_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off) return 1 ;;
    *) return 0 ;;
  esac
}
curator_archive_state() {
  case "$(printf '%s' "${CCC_SKILL_CURATOR_ARCHIVE_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) printf 'ENABLED' ;;
    *) printf 'off' ;;
  esac
}

MODE="${1:-run}"

# --- set-mode: explicit, idempotent mode migration (#2011) -------------------
# The only supported way to move an existing node between modes. It writes the
# canonical value (`review` is stored as `approve`, which every reader —
# this sweep, autoinstall.sh, skill-review.sh — already treats as the human
# gate) atomically, owner-only, and logs the transition. Re-running with the
# same target is a no-op. Runs before the off-switch/autonomy gates on purpose:
# moving TO the human gate must work even while the sweep is paused.
if [ "$MODE" = "set-mode" ]; then
  want="${2:-}"
  case "$want" in
    review|approve) target="approve" ;;
    auto) target="auto" ;;
    *)
      echo "usage: ccc-skill-autosave.sh set-mode review|approve|auto" >&2
      exit 2
      ;;
  esac
  current=""
  [ -f "$MODE_FILE" ] && current="$(head -1 "$MODE_FILE" 2>/dev/null | tr -d '[:space:]')"
  if [ -f "$MODE_FILE" ] && [ "$current" = "$target" ]; then
    echo "set-mode: unchanged ($MODE_FILE already '$target')"
  else
    tmp="$MODE_FILE.tmp.$$"
    if ( umask 077; printf '%s\n' "$target" > "$tmp" ) 2>/dev/null && mv -f "$tmp" "$MODE_FILE" 2>/dev/null; then
      log "set-mode from=${current:-unset} to=$target"
      echo "set-mode: $MODE_FILE ${current:-unset} -> $target"
    else
      rm -f "$tmp" 2>/dev/null
      echo "set-mode: failed to write $MODE_FILE" >&2
      exit 1
    fi
  fi
  if [ -n "${CCC_SKILL_AUTOSAVE_MODE:-}" ] && [ "$(resolve_mode)" != "$target" ]; then
    echo "set-mode: WARNING env CCC_SKILL_AUTOSAVE_MODE=${CCC_SKILL_AUTOSAVE_MODE} overrides the state file; unset it (cron entry / shell profile) for '$target' to take effect" >&2
  fi
  echo "effective mode: $(resolve_mode) (source: $(mode_source))"
  exit 0
fi

if [ "$MODE" = "status" ]; then
  echo "mode: $(resolve_mode) (source: $(mode_source); approve/review = human review before install [default], auto = machine gate + post-hoc notify)"
  [ "$(resolve_mode)" = "auto" ] && echo "advisory: $AUTO_ADVISORY"
  echo "off-switch: $([ -f "$STATE_DIR/skill-autosave.disabled" ] && echo ON || echo off)"
  echo "autonomy: $(declare -f ccc_autonomy_state >/dev/null 2>&1 && ccc_autonomy_state || echo active) (kill = skip whole sweep, dry-run = draft/report only)"
  if curator_enabled; then
    echo "curator: enabled, archive moves $(curator_archive_state) (default mark-only: stale = observation list, archive-candidate = report only; #2011/#1739. CCC_SKILL_CURATOR_ENABLED=false turns it off; first auto run only seeds the interval timer)"
  else
    echo "curator: disabled (CCC_SKILL_CURATOR_ENABLED=${CCC_SKILL_CURATOR_ENABLED:-})"
  fi
  echo "pending skill drafts: $(pending_count)"
  # #1932: sweeps can report drafted_sessions>0 every day while no transcript
  # ever reaches the drafting LLM. skill-review-last.json is written only after
  # a real drafting call, so its age is the honest "is drafting alive" signal.
  last_review="never"
  if [ -f "$STATE_DIR/skill-review-last.json" ]; then
    last_review="$(date -u -r "$STATE_DIR/skill-review-last.json" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
  fi
  echo "last drafting review: $last_review (skill-review-last.json; stale while sweeps log 'review ok' = drafting is stalled, #1932)"
  # Presence only, never the value (#1766): absent on a publisher is exactly the
  # wiring gap that stalled six intake PRs, so it has to be visible at a glance.
  echo "a2a edge env: $([ -f "$EDGE_ENV" ] && echo present || echo absent) ($EDGE_ENV — exports A2A_EDGE_SECRET for the 2d intake review dispatch; publisher only)"
  echo "candidates report: $(ls -la "$STATE_DIR/skill-candidates.md" 2>/dev/null || echo none)"
  echo "-- ledger (last 5) --";      tail -5 "$LEDGER" 2>/dev/null
  echo "-- autosave installs (last 5) --"; tail -5 "$STATE_DIR/skill-autosave-install.jsonl" 2>/dev/null
  echo "-- log (last 10) --";        tail -10 "$LOG" 2>/dev/null
  exit 0
fi

if [ "$MODE" != "run" ]; then
  echo "usage: ccc-skill-autosave.sh [run|status|set-mode review|approve|auto]" >&2
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

# ccc-side-effect: skill_autosave.sweep
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
codex_drafted=0
piri_drafted=0
danso_drafted=0
skipped_unreviewable=0
# #1824: true once the optional cross-branch cap is spent; logs which branch
# stopped so a capped run is distinguishable from an idle one.
total_budget_spent() { # <branch-label>
  [ "$TOTAL_MAX_SESSIONS" -gt 0 ] || return 1
  [ $((drafted + codex_drafted + piri_drafted + danso_drafted)) -ge "$TOTAL_MAX_SESSIONS" ] || return 1
  log "$1 budget-stop reason=total-max-sessions total_max=$TOTAL_MAX_SESSIONS"
  return 0
}
if [ ! -f "$REVIEW" ]; then
  log "review skipped reason=no-skill-review path=$REVIEW"
elif [ -f "$STATE_DIR/skill-review.disabled" ]; then
  log "review skipped reason=skill-review-disabled"
else
  touch "$LEDGER" 2>/dev/null
  before="$(pending_count)"
  while IFS= read -r transcript; do
    [ "$drafted" -ge "$MAX_SESSIONS" ] && break
    total_budget_spent review && break
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
    review_rc=0
    jq -nc --arg sid "$sid" --arg tp "$transcript" \
        '{session_id:$sid, transcript_path:$tp}' 2>/dev/null \
        | CCC_SKILL_REVIEW_STATE_DIR="$STATE_DIR" CCC_SKILL_REVIEW_SKIP_RC="$REVIEW_SKIP_RC" \
            bash "$REVIEW" manual >>"$LOG" 2>&1 || review_rc=$?
    if [ "$review_rc" = 0 ] || [ "$review_rc" = "$REVIEW_SKIP_RC" ]; then
      tmp="$LEDGER.tmp.$$"
      { awk -F'\t' -v s="$sid" '$1!=s' "$LEDGER" 2>/dev/null;
        printf '%s\t%s\t%s\n' "$sid" "$(ts)" "$size"; } > "$tmp" 2>/dev/null \
        && mv "$tmp" "$LEDGER" 2>/dev/null
    fi
    if [ "$review_rc" = 0 ]; then
      drafted=$((drafted + 1))
      log "review ok session=$sid size=$size"
    elif [ "$review_rc" = "$REVIEW_SKIP_RC" ]; then
      skipped_unreviewable=$((skipped_unreviewable + 1))
      log "review skipped session=$sid size=$size reason=not-reviewable (no budget used)"
    else
      log "review failed session=$sid (non-fatal)"
    fi
  done < <(find "$PROJECTS_DIR" -name '*.jsonl' -type f -mtime -"$WINDOW_DAYS" -print0 2>/dev/null \
             | xargs -0 -r ls -t 2>/dev/null)

  # #1867: a non-Claude lane whose opt-in is simply absent (not an explicit
  # CCC_SKILL_<LANE>_DRAFTING=0) while its session tree holds recent sessions is
  # the signature of lost cron baking — a piri node's lane logged the ordinary
  # "not-enabled" skip for nine days. Probe with -quit (first hit only) and
  # surface it as a WARN line below instead of a routine skip.
  lanes_not_enabled=""
  lane_has_recent_sessions() { # <sessions-dir>...
    local _dir
    for _dir in "$@"; do
      [ -d "$_dir" ] || continue
      [ -n "$(find "$_dir" -name '*.jsonl' -type f -mtime -"$WINDOW_DAYS" -print -quit 2>/dev/null)" ] && return 0
    done
    return 1
  }
  note_lane_not_enabled() { # <lane> <explicit-opt-in-value> <sessions-dir>... -> $lane_hint
    local lane="$1" explicit="$2"
    shift 2
    lane_hint=""
    [ -z "$explicit" ] || return 0
    lane_has_recent_sessions "$@" || return 0
    lanes_not_enabled="${lanes_not_enabled:+$lanes_not_enabled,}$lane"
    lane_hint=" recent_sessions=yes"
  }

  # --- 2a) codex branch (#1353, opt-in) --------------------------------------
  # Codex sessions live in $CODEX_HOME/sessions/**/rollout-*.jsonl with a
  # different record shape, so the drafting brain above never sees them. When
  # opted in, each rollout is projected into the Claude transcript shape
  # (codex-rollout-normalize.py) into a branch-local tree, then pushed through
  # the SAME skill-review.sh pipeline with CCC_SKILL_PROVIDER=codex — provider.sh
  # then routes installs to $CODEX_HOME/skills, promotion staging reads the
  # branch provider from .autosave-meta.json, and scan.sh reuses the normalized
  # tree via CLAUDE_PROJECTS_DIR — all unchanged downstream.
  # Default OFF (CCC_SKILL_CODEX_DRAFTING=1, or state file
  # skill-autosave.codex-drafting): nodes without the flag pay nothing — the
  # sessions tree is not walked (only a -quit recency probe, #1867).
  # Machine-driven codex_exec sessions are excluded at projection time
  # (self-reference bias, same as promotion's self-review ban);
  # CCC_SKILL_CODEX_INCLUDE_EXEC=1 lifts it.
  codex_drafted=0
  codex_opt_in="${CCC_SKILL_CODEX_DRAFTING:-}"
  if [ -z "$codex_opt_in" ] && [ -f "$STATE_DIR/skill-autosave.codex-drafting" ]; then
    codex_opt_in=1
  fi
  codex_home="${CODEX_HOME:-${HOME:-/root}/.codex}"
  codex_normalizer="${CCC_SKILL_CODEX_NORMALIZE_CMD:-}"
  if [ -z "$codex_normalizer" ]; then
    for _codex_norm in "$CLAUDE_DIR/hooks/codex-rollout-normalize.py" \
                       "$AUTOSAVE_SELF_DIR/codex-rollout-normalize.py" \
                       "$AUTOSAVE_SELF_DIR/../scripts/codex-rollout-normalize.py"; do
      [ -f "$_codex_norm" ] && { codex_normalizer="$_codex_norm"; break; }
    done
    unset _codex_norm
  fi
  if [ "$codex_opt_in" != "1" ]; then
    note_lane_not_enabled codex "${CCC_SKILL_CODEX_DRAFTING:-}" "$codex_home/sessions"
    log "codex skipped reason=not-enabled$lane_hint"
  elif [ ! -f "$codex_normalizer" ]; then
    log "codex skipped reason=no-normalizer"
  elif [ ! -d "$codex_home/sessions" ]; then
    log "codex skipped reason=no-sessions-tree path=$codex_home/sessions"
  else
    codex_tree="$STATE_DIR/codex-normalized"
    codex_ledger="$STATE_DIR/skill-autosave.codex-seen"
    mkdir -p "$codex_tree" 2>/dev/null
    touch "$codex_ledger" 2>/dev/null
    codex_record_ledger() {
      local tmp="$codex_ledger.tmp.$$"
      { awk -F'\t' -v s="$1" '$1!=s' "$codex_ledger" 2>/dev/null;
        printf '%s\t%s\t%s\n' "$1" "$(ts)" "$2"; } > "$tmp" 2>/dev/null \
        && mv "$tmp" "$codex_ledger" 2>/dev/null
    }
    while IFS= read -r rollout; do
      [ "$codex_drafted" -ge "$MAX_SESSIONS" ] && break
      total_budget_spent codex && break
      [ -f "$rollout" ] || continue
      sid="$(basename "$rollout" .jsonl)"
      size="$(wc -c < "$rollout" 2>/dev/null | tr -d '[:space:]')"
      case "$size" in ''|*[!0-9]*) size=0 ;; esac
      last_size="$(awk -F'\t' -v s="$sid" '$1==s {sz=$3} END {print sz+0}' "$codex_ledger" 2>/dev/null)"
      case "$last_size" in ''|*[!0-9]*) last_size=0 ;; esac
      if [ "$last_size" -gt 0 ] && [ $((size - last_size)) -lt "$REGROWTH_BYTES" ]; then
        continue
      fi
      norm_args=("--out-dir" "$codex_tree")
      [ "${CCC_SKILL_CODEX_INCLUDE_EXEC:-0}" = "1" ] && norm_args+=("--include-exec")
      summary="$(python3 "$codex_normalizer" "$rollout" "${norm_args[@]}" 2>>"$LOG")" || {
        log "codex normalize failed session=$sid (non-fatal)"
        continue
      }
      excluded="$(printf '%s' "$summary" | jq -r '.excluded // false' 2>/dev/null)"
      empty="$(printf '%s' "$summary" | jq -r '.empty // false' 2>/dev/null)"
      out_path="$(printf '%s' "$summary" | jq -r '.out_path // empty' 2>/dev/null)"
      if [ "$excluded" = "true" ]; then
        codex_record_ledger "$sid" "$size"
        log "codex excluded session=$sid reason=codex_exec"
        continue
      fi
      if [ "$empty" = "true" ] || [ -z "$out_path" ] || [ ! -f "$out_path" ]; then
        codex_record_ledger "$sid" "$size"
        log "codex empty projection session=$sid"
        continue
      fi
      # Scoped branch env: provider.sh resolves the codex install target
      # ($CODEX_HOME/skills) from CCC_SKILL_PROVIDER, skill-review.sh re-roots
      # its project discovery at the normalized tree, and the shared
      # CCC_SKILL_REVIEW_STATE_DIR keeps the pending queue and the autoinstall
      # daily-cap ledger summed across both branches (#1353).
      review_rc=0
      jq -nc --arg sid "$sid" --arg tp "$out_path" \
          '{session_id:$sid, transcript_path:$tp}' 2>/dev/null \
          | env CCC_SKILL_PROVIDER=codex \
                CLAUDE_PROJECTS_DIR="$codex_tree" \
                CCC_SKILL_REVIEW_STATE_DIR="$STATE_DIR" \
                CCC_SKILL_REVIEW_SKIP_RC="$REVIEW_SKIP_RC" \
                bash "$REVIEW" manual >>"$LOG" 2>&1 || review_rc=$?
      if [ "$review_rc" = 0 ]; then
        codex_drafted=$((codex_drafted + 1))
        codex_record_ledger "$sid" "$size"
        log "codex review ok session=$sid size=$size"
      elif [ "$review_rc" = "$REVIEW_SKIP_RC" ]; then
        skipped_unreviewable=$((skipped_unreviewable + 1))
        codex_record_ledger "$sid" "$size"
        log "codex review skipped session=$sid size=$size reason=not-reviewable (no budget used)"
      else
        log "codex review failed session=$sid (non-fatal)"
      fi
    done < <(find "$codex_home/sessions" -name '*.jsonl' -type f -mtime -"$WINDOW_DAYS" -print0 2>/dev/null \
               | xargs -0 -r ls -t 2>/dev/null)
    log "codex sweep done drafted_sessions=$codex_drafted"
  fi

  # --- 2b) piri branch (opt-in, mirrors the #1353 codex branch) ---------------
  # Piri (pi) sessions live in $PIRI_CODING_AGENT_DIR/sessions/**/*.jsonl with a
  # different record shape, so the drafting brain above never sees them. When
  # opted in, each session is projected into the Claude transcript shape
  # (piri-session-normalize.py) into a branch-local tree, then pushed through
  # the SAME skill-review.sh pipeline with CCC_SKILL_PROVIDER=piri — provider.sh
  # then routes installs to the piri skills dir, promotion staging reads the
  # branch provider from .autosave-meta.json, and scan.sh reuses the normalized
  # tree via CLAUDE_PROJECTS_DIR — all unchanged downstream.
  # Default OFF (CCC_SKILL_PIRI_DRAFTING=1, or state file
  # skill-autosave.piri-drafting): nodes without the flag pay nothing — the
  # sessions tree is not walked (only a -quit recency probe, #1867).
  piri_drafted=0
  piri_opt_in="${CCC_SKILL_PIRI_DRAFTING:-}"
  if [ -z "$piri_opt_in" ] && [ -f "$STATE_DIR/skill-autosave.piri-drafting" ]; then
    piri_opt_in=1
  fi
  piri_home="${PIRI_CODING_AGENT_DIR:-${HOME:-/root}/.piri/agent}"
  piri_normalizer="${CCC_SKILL_PIRI_NORMALIZE_CMD:-}"
  if [ -z "$piri_normalizer" ]; then
    for _piri_norm in "$CLAUDE_DIR/hooks/piri-session-normalize.py" \
                       "$AUTOSAVE_SELF_DIR/piri-session-normalize.py" \
                       "$AUTOSAVE_SELF_DIR/../scripts/piri-session-normalize.py"; do
      [ -f "$_piri_norm" ] && { piri_normalizer="$_piri_norm"; break; }
    done
    unset _piri_norm
  fi
  if [ "$piri_opt_in" != "1" ]; then
    note_lane_not_enabled piri "${CCC_SKILL_PIRI_DRAFTING:-}" "$piri_home/sessions"
    log "piri skipped reason=not-enabled$lane_hint"
  elif [ ! -f "$piri_normalizer" ]; then
    log "piri skipped reason=no-normalizer"
  elif [ ! -d "$piri_home/sessions" ]; then
    log "piri skipped reason=no-sessions-tree path=$piri_home/sessions"
  else
    piri_tree="$STATE_DIR/piri-normalized"
    piri_ledger="$STATE_DIR/skill-autosave.piri-seen"
    mkdir -p "$piri_tree" 2>/dev/null
    touch "$piri_ledger" 2>/dev/null
    piri_record_ledger() {
      local tmp="$piri_ledger.tmp.$$"
      { awk -F'\t' -v s="$1" '$1!=s' "$piri_ledger" 2>/dev/null;
        printf '%s\t%s\t%s\n' "$1" "$(ts)" "$2"; } > "$tmp" 2>/dev/null \
        && mv "$tmp" "$piri_ledger" 2>/dev/null
    }
    while IFS= read -r session; do
      [ "$piri_drafted" -ge "$MAX_SESSIONS" ] && break
      total_budget_spent piri && break
      [ -f "$session" ] || continue
      sid="$(basename "$session" .jsonl)"
      size="$(wc -c < "$session" 2>/dev/null | tr -d '[:space:]')"
      case "$size" in ''|*[!0-9]*) size=0 ;; esac
      last_size="$(awk -F'\t' -v s="$sid" '$1==s {sz=$3} END {print sz+0}' "$piri_ledger" 2>/dev/null)"
      case "$last_size" in ''|*[!0-9]*) last_size=0 ;; esac
      if [ "$last_size" -gt 0 ] && [ $((size - last_size)) -lt "$REGROWTH_BYTES" ]; then
        continue
      fi
      summary="$(python3 "$piri_normalizer" "$session" --out-dir "$piri_tree" 2>>"$LOG")" || {
        log "piri normalize failed session=$sid (non-fatal)"
        continue
      }
      empty="$(printf '%s' "$summary" | jq -r '.empty // false' 2>/dev/null)"
      out_path="$(printf '%s' "$summary" | jq -r '.out_path // empty' 2>/dev/null)"
      if [ "$empty" = "true" ] || [ -z "$out_path" ] || [ ! -f "$out_path" ]; then
        piri_record_ledger "$sid" "$size"
        log "piri empty projection session=$sid"
        continue
      fi
      # Scoped branch env: provider.sh resolves the piri install target
      # (~/.piri/agent/skills) from CCC_SKILL_PROVIDER, skill-review.sh re-roots
      # its project discovery at the normalized tree, and the shared
      # CCC_SKILL_REVIEW_STATE_DIR keeps the pending queue and the autoinstall
      # daily-cap ledger summed across all branches (codex #1353 precedent).
      review_rc=0
      jq -nc --arg sid "$sid" --arg tp "$out_path" \
          '{session_id:$sid, transcript_path:$tp}' 2>/dev/null \
          | env CCC_SKILL_PROVIDER=piri \
                CLAUDE_PROJECTS_DIR="$piri_tree" \
                CCC_SKILL_REVIEW_STATE_DIR="$STATE_DIR" \
                CCC_SKILL_REVIEW_SKIP_RC="$REVIEW_SKIP_RC" \
                bash "$REVIEW" manual >>"$LOG" 2>&1 || review_rc=$?
      if [ "$review_rc" = 0 ]; then
        piri_drafted=$((piri_drafted + 1))
        piri_record_ledger "$sid" "$size"
        log "piri review ok session=$sid size=$size"
      elif [ "$review_rc" = "$REVIEW_SKIP_RC" ]; then
        skipped_unreviewable=$((skipped_unreviewable + 1))
        piri_record_ledger "$sid" "$size"
        log "piri review skipped session=$sid size=$size reason=not-reviewable (no budget used)"
      else
        log "piri review failed session=$sid (non-fatal)"
      fi
    done < <(find "$piri_home/sessions" -name '*.jsonl' -type f -mtime -"$WINDOW_DAYS" -print0 2>/dev/null \
               | xargs -0 -r ls -t 2>/dev/null)
    log "piri sweep done drafted_sessions=$piri_drafted"
  fi

  # --- 2c) danso branch (opt-in, #1660; mirrors the codex/piri branches) ------
  # Danso session journals are Pi Session JSONL v3 compatible, so the same
  # projector works; the journal tree (<CCC_DANSO_STATE_DIR>/{journals,
  # chatgpt-journals, glm-journals}[-audience/<scope>]/<uuid>.jsonl) replaces
  # piri's encoded-cwd layout, so --project-enc is derived from the journal
  # root + audience scope. Locked journals (danso writes them exclusively) are
  # deferred to a later sweep via the normalizer's shared-lock probe.
  danso_drafted=0
  danso_opt_in="${CCC_SKILL_DANSO_DRAFTING:-}"
  if [ -z "$danso_opt_in" ] && [ -f "$STATE_DIR/skill-autosave.danso-drafting" ]; then
    danso_opt_in=1
  fi
  danso_state="${CCC_DANSO_STATE_DIR:-}"
  if [ "$danso_opt_in" != "1" ]; then
    lane_hint=""
    if [ -n "$danso_state" ]; then
      note_lane_not_enabled danso "${CCC_SKILL_DANSO_DRAFTING:-}" \
        "$danso_state/journals" "$danso_state/journals-audience" \
        "$danso_state/chatgpt-journals" "$danso_state/chatgpt-journals-audience" \
        "$danso_state/glm-journals" "$danso_state/glm-journals-audience"
    fi
    log "danso skipped reason=not-enabled$lane_hint"
  elif [ -z "$danso_state" ]; then
    log "danso skipped reason=no-state-dir"
  else
    danso_normalizer="${CCC_SKILL_DANSO_NORMALIZE_CMD:-}"
    if [ -z "$danso_normalizer" ]; then
      for _danso_norm in "$CLAUDE_DIR/hooks/piri-session-normalize.py" \
                         "$AUTOSAVE_SELF_DIR/piri-session-normalize.py" \
                         "$AUTOSAVE_SELF_DIR/../scripts/piri-session-normalize.py"; do
        [ -f "$_danso_norm" ] && { danso_normalizer="$_danso_norm"; break; }
      done
      unset _danso_norm
    fi
    if [ ! -f "$danso_normalizer" ]; then
      log "danso skipped reason=no-normalizer"
    else
      danso_tree="$STATE_DIR/danso-normalized"
      danso_ledger="$STATE_DIR/skill-autosave.danso-seen"
      mkdir -p "$danso_tree" 2>/dev/null
      touch "$danso_ledger" 2>/dev/null
      danso_record_ledger() {
        local tmp="$danso_ledger.tmp.$$"
        { awk -F'\t' -v s="$1" '$1!=s' "$danso_ledger" 2>/dev/null;
          printf '%s\t%s\t%s\n' "$1" "$(ts)" "$2"; } > "$tmp" 2>/dev/null \
          && mv "$tmp" "$danso_ledger" 2>/dev/null
      }
      while IFS= read -r journal; do
        [ "$danso_drafted" -ge "$MAX_SESSIONS" ] && break
        total_budget_spent danso && break
        [ -f "$journal" ] || continue
        jid="$(basename "$journal" .jsonl)"
        size="$(wc -c < "$journal" 2>/dev/null | tr -d '[:space:]')"
        case "$size" in ''|*[!0-9]*) size=0 ;; esac
        last_size="$(awk -F'\t' -v s="$jid" '$1==s {sz=$3} END {print sz+0}' "$danso_ledger" 2>/dev/null)"
        case "$last_size" in ''|*[!0-9]*) last_size=0 ;; esac
        if [ "$last_size" -gt 0 ] && [ $((size - last_size)) -lt "$REGROWTH_BYTES" ]; then
          continue
        fi
        rel="${journal#"$danso_state"/}"
        project_enc="$(printf '%s' "${rel%/*}" | sed -E 's|[^A-Za-z0-9._]|-|g' | cut -c1-96)"
        summary="$(python3 "$danso_normalizer" "$journal" --out-dir "$danso_tree" --lock --project-enc "$project_enc" 2>>"$LOG")" || {
          log "danso normalize failed session=$jid (non-fatal)"
          continue
        }
        locked="$(printf '%s' "$summary" | jq -r '.locked // false' 2>/dev/null)"
        if [ "$locked" = "true" ]; then
          log "danso journal locked session=$jid (deferred)"
          continue
        fi
        empty="$(printf '%s' "$summary" | jq -r '.empty // false' 2>/dev/null)"
        out_path="$(printf '%s' "$summary" | jq -r '.out_path // empty' 2>/dev/null)"
        if [ "$empty" = "true" ] || [ -z "$out_path" ] || [ ! -f "$out_path" ]; then
          danso_record_ledger "$jid" "$size"
          log "danso empty projection session=$jid"
          continue
        fi
        # Scoped branch env: provider.sh resolves the danso install target
        # (DANSO_SKILLS_DIR / CCC_DANSO_STATE_DIR contract, #1659) from
        # CCC_SKILL_PROVIDER; the shared CCC_SKILL_REVIEW_STATE_DIR keeps the
        # pending queue and the autoinstall daily-cap ledger summed across all
        # branches (codex #1353, piri #1652 precedent).
        review_rc=0
        jq -nc --arg sid "$jid" --arg tp "$out_path" \
            '{session_id:$sid, transcript_path:$tp}' 2>/dev/null \
            | env CCC_SKILL_PROVIDER=danso \
                  CLAUDE_PROJECTS_DIR="$danso_tree" \
                  CCC_SKILL_REVIEW_STATE_DIR="$STATE_DIR" \
                  CCC_SKILL_REVIEW_SKIP_RC="$REVIEW_SKIP_RC" \
                  bash "$REVIEW" manual >>"$LOG" 2>&1 || review_rc=$?
        if [ "$review_rc" = 0 ]; then
          danso_drafted=$((danso_drafted + 1))
          danso_record_ledger "$jid" "$size"
          log "danso review ok session=$jid size=$size"
        elif [ "$review_rc" = "$REVIEW_SKIP_RC" ]; then
          skipped_unreviewable=$((skipped_unreviewable + 1))
          danso_record_ledger "$jid" "$size"
          log "danso review skipped session=$jid size=$size reason=not-reviewable (no budget used)"
        else
          log "danso review failed session=$jid (non-fatal)"
        fi
      done < <(
        for _danso_root in "$danso_state/journals" "$danso_state/journals-audience" \
                            "$danso_state/chatgpt-journals" "$danso_state/chatgpt-journals-audience" \
                            "$danso_state/glm-journals" "$danso_state/glm-journals-audience"; do
          find "$_danso_root" -name '*.jsonl' -type f -mtime -"$WINDOW_DAYS" -print0 2>/dev/null
        done | xargs -0 -r ls -t 2>/dev/null
      )
      log "danso sweep done drafted_sessions=$danso_drafted"
    fi
  fi

  if [ -n "$lanes_not_enabled" ]; then
    log "WARN lanes-not-enabled lanes=$lanes_not_enabled (recent sessions exist but drafting is off; lost cron baking? reinstall with the lane flags or set CCC_SKILL_<LANE>_DRAFTING=0 if deliberate, #1867)"
  fi

  # skill-review.sh stages drafts from a detached background pipeline; give it
  # a bounded window to settle so this run's notification (step 3) can already
  # count fresh drafts. A quiet pipeline (no reusable procedure found) simply
  # times out and the next scheduled run picks up whatever landed later.
  SETTLE="${CCC_SKILL_AUTOSAVE_SETTLE_SECONDS:-90}"
  case "$SETTLE" in ''|*[!0-9]*) SETTLE=90 ;; esac
  # #1652: the piri branch shares the same pending queue, so a piri-only run
  # must also settle before the notification counts fresh drafts.
  if [ $((drafted + codex_drafted + piri_drafted + danso_drafted)) -gt 0 ] && [ "$SETTLE" -gt 0 ]; then
    waited=0
    while [ "$waited" -lt "$SETTLE" ]; do
      [ "$(pending_count)" != "$before" ] && break
      sleep 5; waited=$((waited + 5))
    done
  fi
  after="$(pending_count)"
  log "sweep done drafted_sessions=$drafted codex_drafted=$codex_drafted piri_drafted=$piri_drafted danso_drafted=$danso_drafted pending_before=$before pending_after=$after skipped_unreviewable=$skipped_unreviewable total_drafted=$((drafted + codex_drafted + piri_drafted + danso_drafted)) max_sessions_per_branch=$MAX_SESSIONS total_max_sessions=$TOTAL_MAX_SESSIONS"
fi

# --- 2b) auto mode (#355): machine-gate + install passing drafts -------------
# autoinstall.sh no-ops unless mode=auto; it owns the gates, the daily cap, the
# installed-by=autosave ledger and the post-hoc Telegram notice for installs
# and blocks, so the sweep just invokes it and records the summary.
EFFECTIVE_MODE="$(resolve_mode)"
if [ "$EFFECTIVE_MODE" = "auto" ]; then
  log "mode-advisory mode=auto source=$(mode_source) recommended=review (#2011) migrate='ccc-skill-autosave.sh set-mode review'"
  if [ -f "$AUTOINSTALL" ]; then
    # autoinstall.sh anchors its queue to CCC_SKILL_REVIEW_STATE_DIR, never to
    # CCC_STATE_DIR (which the bridge scopes per memory audience). Hand it the
    # sweep's resolved state dir so both layers read the same queue.
    summary="$(CCC_SKILL_REVIEW_STATE_DIR="$STATE_DIR" CCC_SKILL_AUTOSAVE_TRIGGER=sweep bash "$AUTOINSTALL" run 2>>"$LOG")" \
      && log "autoinstall $(printf '%s' "$summary" | head -c 500)" \
      || log "autoinstall failed (non-fatal)"
  else
    # Fall back to the approve-mode reminder below rather than going silent.
    EFFECTIVE_MODE="approve"
    log "autoinstall skipped reason=missing path=$AUTOINSTALL"
  fi
fi

# --- 2c) curator lifecycle (#752; mark-only by default, #2011) -----------------
# Deterministic lifecycle for autosave-managed skills. ON by default since
# #2011, but only its candidate-marking stage: skills idle past the stale
# window are marked `stale` (observation list) and, after a full recheck
# window, reported as archive candidates — nothing is moved or deleted. The
# archive move stays PR-first + owner approval (#1739) unless
# CCC_SKILL_CURATOR_ARCHIVE_ENABLED=true is set explicitly (not recommended
# before #1648's fleet measurement preconditions hold).
# CCC_SKILL_CURATOR_ENABLED=false turns the curator off. The curator self-
# gates (first-run deferral, interval, min-idle) and never calls a provider.
# Under autonomy dry-run it reports only; kill already exited above.
if ! curator_enabled; then
  log "curator skipped reason=disabled"
else
  if [ ! -f "$CURATOR" ]; then
    log "curator skipped reason=missing path=$CURATOR"
  elif ! command -v python3 >/dev/null 2>&1; then
    log "curator skipped reason=no-python3"
  else
    curator_args="run --auto"
    [ "$AUTONOMY_STATE" = "dry-run" ] && curator_args="run --auto --dry-run"
    summary="$(python3 "$CURATOR" $curator_args 2>>"$LOG")" \
      && log "curator $(printf '%s' "$summary" | head -c 500)" \
      || log "curator failed (non-fatal)"
  fi
fi

# --- 2d) private skill intake (explicit opt-in) ------------------------------
# Every enabled node only stages owner-only local envelopes. A separately
# enabled central publisher may then collect local/SSH outboxes and open bounded
# draft PRs after verifying that fleet-skills is PRIVATE. Non-publisher nodes
# perform no GitHub operation. Fleet autonomy dry-run covers both boundaries.
if [ -f "$PROMOTER" ] && command -v python3 >/dev/null 2>&1; then
  promotion_args="run"
  [ "$AUTONOMY_STATE" = "dry-run" ] && promotion_args="run --dry-run"
  summary="$(python3 "$PROMOTER" $promotion_args 2>>"$LOG")" \
    && log "promotion-stage $(printf '%s' "$summary" | head -c 500)" \
    || log "promotion-stage failed (non-fatal)"
  collect_args="collect"
  [ "$AUTONOMY_STATE" = "dry-run" ] && collect_args="collect --dry-run"
  # #1766: only `collect` dispatches the A2A intake review round, and that needs
  # A2A_EDGE_SECRET. The dedicated intake cron sources the publisher edge env
  # first; this sweep did not, so the promoter's dispatch silently returned
  # dispatch_secret_missing — the intake PR opened and then sat on a2a/receipts
  # FAILURE forever. Load the env for the collect child only (staging never
  # dispatches). Absent file stays non-fatal: an ordinary node has none and
  # reports publisher_enabled=false anyway.
  if [ -f "$EDGE_ENV" ]; then edge_state=loaded; else edge_state=absent; fi
  summary="$(collect_with_edge_env $collect_args)" \
    && log "promotion-collect edge-env=$edge_state $(printf '%s' "$summary" | head -c 500)" \
    || log "promotion-collect failed (non-fatal) edge-env=$edge_state"
else
  log "promotion skipped reason=missing-runtime"
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
    text="스킬 초안 ${pending}건 승인 대기 중 — '/skillsuggest'로 검토/승인하세요."
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
