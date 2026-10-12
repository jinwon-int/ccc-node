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
#   - Always exit 0 when used as a hook. A batch caller (the autosave sweep)
#     may set CCC_SKILL_REVIEW_SKIP_RC=<1-125> to get that code back instead
#     when the transcript is not reviewable (too few turns), so it can tell a
#     skip from a drafting dispatch and not charge its budget for it (#1932).
#   - Recursion guard prevents child `claude -p` sessions from re-firing hooks.
#   - Redaction happens in skill-review/extract.sh before model input.
#   - Off-switch: touch ~/.claude/state/skill-review.disabled
#   - Cooldown: CCC_SKILL_REVIEW_COOLDOWN_SECONDS (default 3600) for hook triggers.
set -uo pipefail

# Node-global anchor, NOT CCC_STATE_DIR. This hook fires from SessionEnd, so
# under an audience-scoped bridge session CCC_STATE_DIR points at a per-audience
# memory tree — staging drafts there would hide them from the collector and the
# installer, both of which use the node-global queue, and would silently move
# the documented off-switch at ~/.claude/state/skill-review.disabled. Skills
# install to one node-wide skills dir, so the whole queue is node-global.
STATE_DIR="${CCC_SKILL_REVIEW_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/skill-review.log"
PENDING_DIR="$STATE_DIR/pending-skills"
mkdir -p "$STATE_DIR" "$PENDING_DIR" 2>/dev/null

TRIGGER="${1:-manual}"
SKILL_REVIEW_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || SKILL_REVIEW_LIB_DIR="${HOME:-/root}/.claude/hooks"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$SKILL_REVIEW_LIB_DIR/lib/hook-common.sh" || exit 0

# --- reindex (#2283): rebuild pending-skills/index.jsonl from the queue -------
# The index is the one cross-draft ledger, but the per-draft append never ran
# (missing `jq -n`, fixed above at the staging site), so every node carries a
# 0-byte index. This rebuilds it from what is actually on disk — one row per
# draft directory — so the fleet usage report can read a lifecycle table
# without SSH-ing into each node. Status comes from the directory-name suffix
# the approve/install/reject paths already use (`.approved-<ts>` /
# `.installed-<ts>` / `.rejected-<ts>`; none = pending) and the prescreen
# verdict is carried along when present. Rows for drafts that already left the
# queue come from the archive manifests (status `archived:<kind>`).
# Atomic: written to a temp file in the same directory, then renamed; a
# previous non-empty index is kept beside it as index.jsonl.bak-<ts>. Prints a
# one-line JSON summary; exit 1 only when the queue directory is unwritable.
# `CCC_SKILL_REVIEW_PENDING_DIR` and `CCC_SKILL_REVIEW_STATE_DIR` are honoured
# like everywhere else in this hook. The sweep runs it after drafting; it is
# also a manual entry point: `skill-review.sh reindex`.
skill_review_reindex() {
  local dir="${CCC_SKILL_REVIEW_PENDING_DIR:-$PENDING_DIR}" index tmp entry base id status verdict rows=0
  local n_pending=0 n_approved=0 n_installed=0 n_rejected=0 n_archived=0 manifest kind now
  index="$dir/index.jsonl"
  mkdir -p "$dir" 2>/dev/null
  tmp="$(mktemp "$dir/.index.jsonl.XXXXXX" 2>/dev/null)" || {
    log "reindex failed reason=unwritable dir=$dir"
    printf '{"status":"error","code":"unwritable","dir":"%s"}\n' "$dir"
    return 1
  }
  now="$(ts)"
  for entry in "$dir"/*/; do
    [ -d "$entry" ] || continue
    base="$(basename "$entry")"
    case "$base" in .*) continue ;; esac
    [ -f "$entry/meta.json" ] || [ -f "$entry/SKILL.md" ] || continue
    case "$base" in
      *.approved-*)  status=approved;  id="${base%.approved-*}";  n_approved=$((n_approved + 1)) ;;
      *.installed-*) status=installed; id="${base%.installed-*}"; n_installed=$((n_installed + 1)) ;;
      *.rejected-*)  status=rejected;  id="${base%.rejected-*}";  n_rejected=$((n_rejected + 1)) ;;
      *)             status=pending;   id="$base";                n_pending=$((n_pending + 1)) ;;
    esac
    verdict=""
    [ -f "$entry/prescreen.json" ] && verdict="$(jq -r '.verdict // empty' "$entry/prescreen.json" 2>/dev/null)"
    if [ -f "$entry/meta.json" ] && jq -e 'type == "object"' "$entry/meta.json" >/dev/null 2>&1; then
      jq -c --arg id "$id" --arg dir "$base" --arg status "$status" --arg verdict "$verdict" --arg at "$now" \
        '{id:$id, name:(.name // $id), status:$status, session_id:(.session_id // null), trigger:(.trigger // null),
          staged_at:(.staged_at // null), provider:(.provider // null), dir:$dir,
          prescreen:(if $verdict == "" then null else $verdict end), reindexed_at:$at}' \
        "$entry/meta.json" >> "$tmp" 2>/dev/null \
        || jq -nc --arg id "$id" --arg dir "$base" --arg status "$status" --arg at "$now" \
             '{id:$id,name:$id,status:$status,dir:$dir,reindexed_at:$at,meta:"unreadable"}' >> "$tmp"
    else
      jq -nc --arg id "$id" --arg dir "$base" --arg status "$status" --arg at "$now" \
        '{id:$id,name:$id,status:$status,dir:$dir,reindexed_at:$at,meta:"missing"}' >> "$tmp"
    fi
    rows=$((rows + 1))
  done
  # Drafts that left the queue: one row per archive manifest entry (#2184
  # expiry, #2183 prescreen reject, #2239 draft-rejected share the root).
  for manifest in "$STATE_DIR"/skill-autosave-archive/*/manifest.jsonl; do
    [ -f "$manifest" ] || continue
    kind="$(basename "$(dirname "$manifest")")"
    kind="${kind%-20[0-9][0-9][0-9][0-9][0-9][0-9]}"
    if jq -c --arg kind "$kind" --arg at "$now" \
         'select(type == "object" and (.name // "") != "" and (.restored_to // "") == "")
          | {id:(.name | sub("\\.(approved|installed|rejected)-[0-9]+$"; "")), name:(.name | sub("^[0-9]{8}-[0-9]{6}-"; "") | sub("\\.(approved|installed|rejected)-[0-9]+$"; "")),
             status:("archived:" + $kind), dir:.name, archived_at:(.ts // null), reason:(.reason // null), reindexed_at:$at}' \
         "$manifest" >> "$tmp" 2>/dev/null; then
      n_archived=$((n_archived + $(jq -c 'select(type == "object" and (.name // "") != "" and (.restored_to // "") == "")' "$manifest" 2>/dev/null | wc -l | tr -d '[:space:]')))
    fi
  done
  if [ -s "$index" ]; then
    cp -p "$index" "$index.bak-$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true
  fi
  chmod 0644 "$tmp" 2>/dev/null || true
  if ! mv -f "$tmp" "$index" 2>/dev/null; then
    rm -f "$tmp" 2>/dev/null
    log "reindex failed reason=rename dir=$dir"
    printf '{"status":"error","code":"rename","dir":"%s"}\n' "$dir"
    return 1
  fi
  log "reindex ok rows=$rows pending=$n_pending approved=$n_approved installed=$n_installed rejected=$n_rejected archived=$n_archived index=$index"
  jq -nc --arg index "$index" --argjson rows "$rows" --argjson p "$n_pending" --argjson a "$n_approved" \
    --argjson i "$n_installed" --argjson r "$n_rejected" --argjson ar "$n_archived" \
    '{status:"reindexed",index:$index,rows:$rows,pending:$p,approved:$a,installed:$i,rejected:$r,archived:$ar}'
  return 0
}

if [ "$TRIGGER" = "reindex" ]; then
  skill_review_reindex
  exit $?
fi

# The emergency off-switch must win even when a SessionEnd hook inherits the
# detached runner marker from its parent `claude -p` process.
if [ -f "$STATE_DIR/skill-review.disabled" ]; then
  log "skip reason=disabled trigger=$TRIGGER pid=$$"
  exit 0
fi

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

  # #1654: record which provider branch staged this draft (claude|codex|piri)
  # so downstream layers can route it — autoinstall per-draft install targets
  # (#1655) and promotion staging read meta.json instead of guessing from the
  # process environment. Provider resolution is the shared provider.sh contract.
  local STAGE_PROVIDER="claude"
  if [ -r "$HOOKDIR/skill-review/provider.sh" ]; then
    # shellcheck source=claude/hooks/skill-review/provider.sh
    . "$HOOKDIR/skill-review/provider.sh"
    if declare -f ccc_skill_provider >/dev/null 2>&1; then
      STAGE_PROVIDER="$(ccc_skill_provider)"
    fi
  fi

  # Draft-time gates (#2239). Until now a draft only had to carry a name and a
  # frontmatter fence to enter pending-skills/; trigger wording and duplication
  # were caught downstream (autoinstall in auto mode, the fleet intake reviewer
  # in review mode), where 85%+ of verdicts came back "revise" for exactly those
  # two reasons. The same checks now run before staging. A failing draft is
  # never deleted: it goes to skill-autosave-archive/draft-rejected/<id>/ with
  # a reject.json so a human can inspect or restore it.
  #   CCC_SKILL_REVIEW_DRAFT_GATES=off      bypass all three gates (default on)
  #   CCC_SKILL_REVIEW_MAX_CANDIDATES=N     staged drafts per session (default
  #                                         1; 0 = no cap)
  #   CCC_SKILL_DEDUP_*                     thresholds, see gates-lib.sh
  local GATES=1 MAX_CANDIDATES REJECT_ROOT WORK DESC_TRIGGER_TOOL
  is_disabled "${CCC_SKILL_REVIEW_DRAFT_GATES:-on}" && GATES=0
  MAX_CANDIDATES="${CCC_SKILL_REVIEW_MAX_CANDIDATES:-1}"
  case "$MAX_CANDIDATES" in ''|*[!0-9]*) MAX_CANDIDATES=1 ;; esac
  REJECT_ROOT="${CCC_SKILL_REVIEW_REJECT_DIR:-$STATE_DIR/skill-autosave-archive/draft-rejected}"
  DESC_TRIGGER_TOOL="$HOOKDIR/skill-review/description_trigger.py"
  if [ "$GATES" = 1 ]; then
    if [ -r "$HOOKDIR/skill-review/gates-lib.sh" ]; then
      # shellcheck source=claude/hooks/skill-review/gates-lib.sh
      . "$HOOKDIR/skill-review/gates-lib.sh"
    fi
    # A stale deployed tree without the lib (or its frontmatter reader) cannot
    # run the gates; say so once and stage as before rather than reject blind.
    if ! declare -f ccc_fm_field >/dev/null 2>&1 || ! declare -f ccc_skill_dedup_installed >/dev/null 2>&1; then
      log "draft-gates off reason=gates-lib-missing trigger=$TRIGGER"
      GATES=0
    fi
  fi
  local INSTALLED_ROOT=""
  if declare -f ccc_skills_dir >/dev/null 2>&1; then
    INSTALLED_ROOT="$(ccc_skills_dir "$STAGE_PROVIDER")"
  else
    INSTALLED_ROOT="${CLAUDE_SKILLS_DIR:-}"
  fi
  local rej_trigger=0 rej_duplicate=0 rej_cap=0

  local PIPE_PID OUT ec STASH count i staged item name skill_md id safe_id dest fm_tool
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
    # The draft is built in an owner-only work dir first (#2239): the gates
    # need the normalized description, and a rejected draft must never have
    # touched pending-skills/ (the collector and /skillsuggest read it live).
    WORK="$STATE_DIR/.skill-review-work-$PIPE_PID"
    rm -rf "$WORK" 2>/dev/null
    ( umask 077; mkdir -p "$WORK" ) 2>/dev/null || { i=$((i + 1)); continue; }
    printf '%s\n' "$skill_md" > "$WORK/SKILL.md"
    # YAML-safe description line (#2032) so a human `/skillsuggest` approve
    # copies a draft fleet-skills can parse. Best-effort: autoinstall.sh
    # re-normalizes (fail-closed) before any automatic install.
    fm_tool="$HOOKDIR/ccc_skill_frontmatter.py"
    [ -r "$fm_tool" ] || fm_tool="$HOOKDIR/../../bridge/utils/skill_frontmatter.py"
    if [ -r "$fm_tool" ]; then
      python3 "$fm_tool" normalize "$WORK/SKILL.md" >/dev/null 2>&1 \
        || log "candidate normalize-failed name=$name trigger=$TRIGGER pid=$PIPE_PID"
    fi
    if [ "$GATES" = 1 ]; then
      local reason="" match="" desc=""
      desc="$(ccc_fm_field "$WORK/SKILL.md" description)"
      if ! skill_review_gate_trigger "$desc"; then
        reason="description-trigger"
      elif ! match="$(skill_review_gate_duplicate "$name" "$desc" "$WORK")"; then
        reason="duplicate"
      elif [ "$MAX_CANDIDATES" -gt 0 ] && [ "$staged" -ge "$MAX_CANDIDATES" ]; then
        reason="session-cap"
      fi
      if [ -n "$reason" ]; then
        skill_review_reject_draft "$safe_id" "$name" "$reason" "$match" "$WORK/SKILL.md" "$item"
        case "$reason" in
          description-trigger) rej_trigger=$((rej_trigger + 1)) ;;
          duplicate) rej_duplicate=$((rej_duplicate + 1)) ;;
          session-cap) rej_cap=$((rej_cap + 1)) ;;
        esac
        rm -rf "$WORK" 2>/dev/null
        i=$((i + 1)); continue
      fi
    fi
    mkdir -p "$dest" 2>/dev/null || { rm -rf "$WORK" 2>/dev/null; i=$((i + 1)); continue; }
    mv -f "$WORK/SKILL.md" "$dest/SKILL.md" 2>/dev/null || { rm -rf "$WORK" 2>/dev/null; i=$((i + 1)); continue; }
    rm -rf "$WORK" 2>/dev/null
    printf '%s' "$item" | jq -c \
      --arg id "$safe_id" \
      --arg session "$SESSION_ID" \
      --arg trigger "$TRIGGER" \
      --arg source_cwd "$SOURCE_CWD" \
      --arg source_project "$PROJECT_ENC" \
      --arg staged_at "$(ts)" \
      --arg skill_path "${CLAUDE_SKILLS_DIR%/}/$name/SKILL.md" \
      --arg provider "$STAGE_PROVIDER" \
      'del(.skill_md) + {id:$id,status:"pending",session_id:$session,trigger:$trigger,source_cwd:$source_cwd,source_project:$source_project,staged_at:$staged_at,target_skill_path:$skill_path,provider:$provider}' \
      > "$dest/meta.json" 2>/dev/null || true
    # #2283: `-n` is load-bearing. Without it jq reads stdin, which the hook
    # entry already drained (HOOK_INPUT="$(cat)"), so it emitted nothing and
    # index.jsonl stayed 0 bytes on every node from #183 (2026-06-26) on.
    jq -nc --arg id "$safe_id" --arg name "$name" --arg session "$SESSION_ID" --arg trigger "$TRIGGER" --arg at "$(ts)" --arg provider "$STAGE_PROVIDER" \
      '{id:$id,name:$name,status:"pending",session_id:$session,trigger:$trigger,staged_at:$at,provider:$provider}' \
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
  # Gate tallies into the stash (#2239) so the sweep and a human can see how
  # many candidates the draft-time gates turned away and why. Rewritten
  # atomically beside the stash; a jq failure leaves the raw stash in place.
  if jq -c --argjson staged "$staged" --argjson t "$rej_trigger" --argjson d "$rej_duplicate" --argjson c "$rej_cap" \
       --arg gates "$([ "$GATES" = 1 ] && printf on || printf off)" \
       '. + {staged:$staged, draft_gates:$gates, rejected:{trigger:$t, duplicate:$d, session_cap:$c}}' \
       "$STASH" > "$STASH.tmp" 2>/dev/null; then
    mv -f "$STASH.tmp" "$STASH" 2>/dev/null || rm -f "$STASH.tmp" 2>/dev/null
  else
    rm -f "$STASH.tmp" 2>/dev/null
  fi
  log "done staged=$staged rejected_trigger=$rej_trigger rejected_duplicate=$rej_duplicate rejected_session_cap=$rej_cap gates=$([ "$GATES" = 1 ] && printf on || printf off) trigger=$TRIGGER pid=$PIPE_PID"
  return 0
}

# --- draft-time gate helpers (#2239) -----------------------------------------
# Called from inside run_skill_review_bg, so they see its locals (bash dynamic
# scoping): DESC_TRIGGER_TOOL, INSTALLED_ROOT, PENDING_DIR, REJECT_ROOT,
# SESSION_ID, TRIGGER, PIPE_PID, STAGE_PROVIDER.

# skill_review_gate_trigger <description> — 0 when the description carries
# trigger wording (description_trigger.py, the fleet-skills TRIGGER_RE copy),
# 1 when it does not. Unlike the install gate this one passes through when the
# check itself cannot run (no python3 / helper missing): it is a pre-filter,
# and autoinstall + promotion still fail closed behind it. The pass-through is
# logged so a node silently skipping the gate is visible.
skill_review_gate_trigger() {
  local rc
  if ! command -v python3 >/dev/null 2>&1 || [ ! -r "${DESC_TRIGGER_TOOL:-}" ]; then
    log "draft-gate description-trigger unverifiable (python3 or helper missing) pass-through pid=${PIPE_PID:-$$}"
    return 0
  fi
  printf '%s' "$1" | python3 "$DESC_TRIGGER_TOOL" check >/dev/null 2>&1
  rc=$?
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) log "draft-gate description-trigger unverifiable rc=$rc pass-through pid=${PIPE_PID:-$$}"; return 0 ;;
  esac
}

# skill_review_gate_duplicate <name> <description> <workdir> — 0 when nothing
# installed (INSTALLED_ROOT, the drafting provider's skills dir) or waiting in
# PENDING_DIR resembles the candidate. On a hit prints
# "<installed|pending> <already-exists|name-similar|description-similar> <name>"
# and returns 1. Passes through (logged) when gates-lib.sh did not load.
skill_review_gate_duplicate() {
  local verdict
  if ! declare -f ccc_skill_dedup_installed >/dev/null 2>&1; then
    log "draft-gate duplicate unverifiable (gates-lib.sh missing) pass-through pid=${PIPE_PID:-$$}"
    return 0
  fi
  if ! verdict="$(ccc_skill_dedup_installed "$1" "$2" "$3" "${INSTALLED_ROOT:-}")"; then
    printf 'installed %s' "$verdict"; return 1
  fi
  if ! verdict="$(ccc_skill_dedup_pending "$1" "$2" "$3" "${PENDING_DIR:-}")"; then
    printf 'pending %s' "$verdict"; return 1
  fi
  return 0
}

# skill_review_reject_draft <id> <name> <reason> <match> <skill.md> <item-json>
#   Moves a gate-rejected draft to REJECT_ROOT/<id>/ (dirs 0700, files 0600):
#   SKILL.md (normalized), meta.json (the candidate record, status=rejected)
#   and reject.json {reason, session, ts, name, id, trigger, provider [,
#   matched, match{scope,kind}]}; appends a manifest.jsonl row in the
#   pending-expire shape so `ccc-skill-autosave.sh pending-restore <id>` can
#   put it back into the queue. Nothing is ever deleted here.
skill_review_reject_draft() {
  local id="$1" name="$2" reason="$3" match="$4" src="$5" item="$6"
  local dir="${REJECT_ROOT:-}/$id" scope="" kind="" matched="" now
  now="$(ts)"
  if [ -n "$match" ]; then
    read -r scope kind matched <<<"$match"
  fi
  if [ -z "${REJECT_ROOT:-}" ] || ! ( umask 077; mkdir -p "$dir" ) 2>/dev/null; then
    log "candidate rejected reason=$reason name=$name id=$id archive=failed trigger=${TRIGGER:-} pid=${PIPE_PID:-$$}"
    return 1
  fi
  chmod 700 "$dir" 2>/dev/null || true
  mv -f "$src" "$dir/SKILL.md" 2>/dev/null || cp -f "$src" "$dir/SKILL.md" 2>/dev/null || true
  chmod 600 "$dir/SKILL.md" 2>/dev/null || true
  printf '%s' "$item" | jq -c \
    --arg id "$id" --arg session "${SESSION_ID:-unknown}" --arg trigger "${TRIGGER:-}" \
    --arg at "$now" --arg provider "${STAGE_PROVIDER:-}" --arg reason "$reason" \
    'del(.skill_md) + {id:$id,status:"rejected",reject_reason:$reason,session_id:$session,trigger:$trigger,staged_at:$at,provider:$provider}' \
    > "$dir/meta.json" 2>/dev/null || true
  jq -nc --arg reason "$reason" --arg session "${SESSION_ID:-unknown}" --arg ts "$now" \
    --arg name "$name" --arg id "$id" --arg trigger "${TRIGGER:-}" --arg provider "${STAGE_PROVIDER:-}" \
    --arg scope "$scope" --arg kind "$kind" --arg matched "$matched" \
    '{reason:$reason, session:$session, ts:$ts, name:$name, id:$id, trigger:$trigger, provider:$provider}
     + (if $matched != "" then {matched:$matched, match:{scope:$scope, kind:$kind}} else {} end)' \
    > "$dir/reject.json" 2>/dev/null || true
  jq -nc --arg ts "$now" --arg name "$id" --arg reason "$reason" --arg skill "$name" \
    --arg session "${SESSION_ID:-unknown}" --arg matched "$matched" --arg node "${CCC_NODE:-}" \
    '{ts:$ts, node:$node, name:$name, reason:("draft-gate:" + $reason), skill:$skill, session:$session}
     + (if $matched != "" then {matched:$matched} else {} end)' \
    >> "$REJECT_ROOT/manifest.jsonl" 2>/dev/null || true
  chmod 600 "$dir/meta.json" "$dir/reject.json" "$REJECT_ROOT/manifest.jsonl" 2>/dev/null || true
  if [ -n "$matched" ]; then
    log "candidate rejected reason=$reason name=$name id=$id matched=$matched match=$scope/$kind trigger=${TRIGGER:-} pid=${PIPE_PID:-$$}"
  else
    log "candidate rejected reason=$reason name=$name id=$id trigger=${TRIGGER:-} pid=${PIPE_PID:-$$}"
  fi
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
  # #1932: hooks keep exit 0; only an explicit batch caller asks for a code.
  case "${CCC_SKILL_REVIEW_SKIP_RC:-}" in
    [1-9]|[1-9][0-9]|1[01][0-9]|12[0-5]) exit "$CCC_SKILL_REVIEW_SKIP_RC" ;;
  esac
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
