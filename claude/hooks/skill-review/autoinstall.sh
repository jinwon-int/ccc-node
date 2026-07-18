#!/usr/bin/env bash
# skill-review/autoinstall.sh — Hermes-style unattended skill installation (#355).
#
# In approve mode (the default) this script is a no-op: drafts staged under
# ~/.claude/state/pending-skills/ wait for a human (/skill-suggest). In auto
# mode (CCC_SKILL_AUTOSAVE_MODE=auto, or `auto` in the skill-autosave.mode
# state file) it replaces the human gate with deterministic machine gates and
# installs passing drafts straight into ~/.claude/skills/, Hermes-style:
# narrow write surface + authoring standards + after-the-fact visibility.
#
# Trust model (mirrors hermes-agent background_review.py):
#   - Write surface is ONLY $CLAUDE_SKILLS_DIR/<kebab-name>/ (never overwrite,
#     never delete outside rollback archive) + its own state files.
#   - Gates fail CLOSED: any gate failure leaves the draft pending for the
#     normal human review path; nothing is dropped.
#   - Every install is recorded in an installed-by=autosave ledger and marked
#     inside the skill dir (.autosave-meta.json) so it can be rolled back,
#     individually or in bulk, and audited later.
#   - Owner is notified AFTER the fact via the redaction-safe Telegram spool
#     (bridge PushNotifier delivers; this script never touches the bot token).
#   - Daily install cap (CCC_SKILL_AUTOSAVE_DAILY_CAP, default 3); over-cap
#     drafts stay pending and are retried on a later run.
#   - Off-switch: touch ~/.claude/state/skill-autosave.disabled
#   - Template-repo (ccc-node) changes remain PR-first — this installs to the
#     local node only.
#
# Verbs:
#   run                 gate + install pending drafts (no-op unless mode=auto)
#   list                ledger, currently-installed autosave skills, blocked drafts
#   rollback <name>     archive an autosave-installed skill (undo)
#   rollback --all      archive every autosave-installed skill
#   status              one-screen mode/cap/ledger summary
set -uo pipefail
export LC_ALL=C

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$CLAUDE_DIR/skills}"
PENDING_DIR="${CCC_SKILL_REVIEW_PENDING_DIR:-$STATE_DIR/pending-skills}"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"
LOG="$STATE_DIR/skill-autoinstall.log"
LEDGER="$STATE_DIR/skill-autosave-install.jsonl"
ROLLBACK_DIR="$STATE_DIR/skill-autosave-rollback"
MODE_FILE="$STATE_DIR/skill-autosave.mode"

DAILY_CAP="${CCC_SKILL_AUTOSAVE_DAILY_CAP:-3}"
NOTIFY="${CCC_SKILL_AUTOSAVE_NOTIFY:-1}"
TRIGGER="${CCC_SKILL_AUTOSAVE_TRIGGER:-manual}"
case "$DAILY_CAP" in ''|*[!0-9]*) DAILY_CAP=3 ;; esac

KEBAB='^[a-z0-9]+(-[a-z0-9]+)*$'
DESC_MIN=20
DESC_MAX=1024
BODY_MIN_LINES=5
DUP_JACCARD_PCT=60
DUP_MIN_UNION=6

mkdir -p "$STATE_DIR" 2>/dev/null
AUTOINSTALL_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || AUTOINSTALL_LIB_DIR="${HOME:-/root}/.claude/hooks/skill-review"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$AUTOINSTALL_LIB_DIR/../lib/hook-common.sh" || exit 0
ts_id() { date -u +%Y%m%d%H%M%S; }

resolve_mode() {
  local m="${CCC_SKILL_AUTOSAVE_MODE:-}"
  if [ -z "$m" ] && [ -f "$MODE_FILE" ]; then
    m="$(head -1 "$MODE_FILE" 2>/dev/null | tr -d '[:space:]')"
  fi
  case "$m" in auto) printf 'auto' ;; *) printf 'approve' ;; esac
}

file_sha() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  else cksum "$1" 2>/dev/null | awk '{print "cksum:"$1}'
  fi
}

fm_field() { # <skill.md> <key> — first single-line frontmatter value
  awk -v k="$2" 'NR==1{next} /^---/{exit} $0 ~ "^"k":" {sub("^"k":[[:space:]]*", ""); print; exit}' "$1" 2>/dev/null
}

fm_close_line() { awk 'NR>1 && /^---[[:space:]]*$/{print NR; exit}' "$1" 2>/dev/null; }

pending_drafts() {
  find "$PENDING_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
    | grep -Ev '\.(approved|rejected|installed)-[0-9]+$' | sort
}

pending_count() { pending_drafts | grep -c . ; }

installs_today() {
  jq -r 'select(.event=="install") | .ts' "$LEDGER" 2>/dev/null | grep -c "^$(date -u +%F)"
}

# ---------------------------------------------------------------------------
# Machine quality gates. Each returns 0 (pass) or prints "reason detail" and
# returns 1. Detail never quotes draft content — pattern labels/names only, so
# logs, meta markers and notifications stay redaction-safe.
# ---------------------------------------------------------------------------

gate_lint() { # <skill.md>
  local f="$1" close name desc body_lines
  [ -f "$f" ] || { printf 'lint missing-skill-md'; return 1; }
  head -1 "$f" | grep -q '^---' || { printf 'lint no-frontmatter'; return 1; }
  close="$(fm_close_line "$f")"
  [ -n "$close" ] || { printf 'lint unterminated-frontmatter'; return 1; }
  name="$(fm_field "$f" name)"
  [ -n "$name" ] || { printf 'lint missing-name'; return 1; }
  printf '%s' "$name" | grep -qE "$KEBAB" || { printf 'lint name-not-kebab'; return 1; }
  [ "${#name}" -le 64 ] || { printf 'lint name-too-long'; return 1; }
  desc="$(fm_field "$f" description)"
  [ -n "$desc" ] || { printf 'lint missing-description'; return 1; }
  [ "${#desc}" -ge "$DESC_MIN" ] || { printf 'lint description-too-short'; return 1; }
  [ "${#desc}" -le "$DESC_MAX" ] || { printf 'lint description-too-long'; return 1; }
  body_lines="$(awk -v s="$close" 'NR>s && NF' "$f" 2>/dev/null | wc -l | tr -d '[:space:]')"
  [ "${body_lines:-0}" -ge "$BODY_MIN_LINES" ] || { printf 'lint body-too-short'; return 1; }
  awk -v s="$close" 'NR>s' "$f" 2>/dev/null | grep -q '^#' || { printf 'lint no-headings'; return 1; }
  return 0
}

gate_secrets() { # <skill.md> — reuses the redaction scanner pattern family
  local f="$1" p label rx
  local patterns=(
    'gh-token::(ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}'
    'api-key::sk-[A-Za-z0-9_-]{20,}'
    'aws-key::AKIA[A-Z0-9]{16}'
    'private-key::-----BEGIN [A-Z ]*PRIVATE KEY-----'
    'bearer::Bearer [A-Za-z0-9._~+/=-]{20,}'
    'redaction-marker::\[REDACTED'
    'credential-assignment::(password|passwd|secret|token|api[_-]?key|authorization)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9+/_-]{16,}'
    'possible-token::[A-Za-z0-9+/]{40,}'
  )
  for p in "${patterns[@]}"; do
    label="${p%%::*}"; rx="${p#*::}"
    if grep -qiE "$rx" "$f" 2>/dev/null; then printf 'secret %s' "$label"; return 1; fi
  done
  return 0
}

gate_node_specific() { # <skill.md> — hardcoded node facts stay human-reviewed
  local f="$1" hit
  if grep -qE '(/home|/Users)/[A-Za-z0-9._-]+' "$f" 2>/dev/null; then
    printf 'node-specific home-path'; return 1
  fi
  if grep -qE '(^|[^A-Za-z0-9_])/root/' "$f" 2>/dev/null; then
    printf 'node-specific root-path'; return 1
  fi
  hit="$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' "$f" 2>/dev/null \
    | grep -Ev '^(127\.|0\.0\.0\.0$)' | head -1)"
  if [ -n "$hit" ]; then printf 'node-specific ipv4'; return 1; fi
  hit="$(grep -oiE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "$f" 2>/dev/null \
    | tr '[:upper:]' '[:lower:]' | grep -Fvx -e 'git@github.com' -e 'git@gitlab.com' | head -1)"
  if [ -n "$hit" ]; then printf 'node-specific user-at-host'; return 1; fi
  return 0
}

norm_name() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -dc 'a-z0-9'; }

desc_tokens() { # <text> — one lowercase token (len>=3) per line, unique+sorted
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '\n' \
    | awk 'length($0) >= 3' | sort -u
}

gate_dedup() { # <name> <description> <workdir>
  local name="$1" desc="$2" work="$3" f ex_name ex_desc inter union
  if [ -e "$SKILLS_DIR/$name" ]; then printf 'dedup already-exists %s' "$name"; return 1; fi
  desc_tokens "$desc" > "$work/draft.tok"
  while IFS= read -r f; do
    ex_name="$(fm_field "$f" name)"
    [ -n "$ex_name" ] || continue
    if [ "$(norm_name "$ex_name")" = "$(norm_name "$name")" ]; then
      printf 'dedup name-similar %s' "$ex_name"; return 1
    fi
    ex_desc="$(fm_field "$f" description)"
    [ -n "$ex_desc" ] || continue
    desc_tokens "$ex_desc" > "$work/exist.tok"
    inter="$(comm -12 "$work/draft.tok" "$work/exist.tok" 2>/dev/null | wc -l | tr -d '[:space:]')"
    union="$(sort -u "$work/draft.tok" "$work/exist.tok" 2>/dev/null | wc -l | tr -d '[:space:]')"
    if [ "${union:-0}" -ge "$DUP_MIN_UNION" ] \
       && [ $((inter * 100)) -ge $((union * DUP_JACCARD_PCT)) ]; then
      printf 'dedup description-similar %s' "$ex_name"; return 1
    fi
  done < <(find "$SKILLS_DIR" -maxdepth 2 -name SKILL.md 2>/dev/null | sort)
  return 0
}

# ---------------------------------------------------------------------------
# run — gate + install
# ---------------------------------------------------------------------------

mark_blocked() { # <draft-dir> <reason> ; echoes "new" when first seen with this reason
  local reason="$2" marker="$1/autosave-block.json" prev=""
  [ -f "$marker" ] && prev="$(jq -r '.reason // empty' "$marker" 2>/dev/null)"
  jq -nc --arg reason "$reason" --arg at "$(ts)" '{reason:$reason, at:$at}' \
    > "$marker" 2>/dev/null || true
  [ "$prev" = "$reason" ] || printf 'new'
}

do_run() {
  local mode summary
  mode="$(resolve_mode)"
  if ! command -v jq >/dev/null 2>&1; then
    log "skip reason=no-jq trigger=$TRIGGER"
    printf '{"mode":"%s","skipped":"no-jq"}\n' "$mode"
    return 0
  fi
  if [ "$mode" != "auto" ]; then
    jq -nc --arg mode "$mode" --argjson pending "$(pending_count)" \
      '{mode:$mode, skipped:"mode", pending:$pending}'
    return 0
  fi
  if [ -f "$STATE_DIR/skill-autosave.disabled" ]; then
    log "skip reason=disabled trigger=$TRIGGER"
    printf '{"mode":"auto","skipped":"disabled"}\n'
    return 0
  fi

  local work dir id f name desc verdict rec sha sid dest
  local -a installed=() blocked=() newly_blocked=()
  local deferred=0 today_used
  work="$(mktemp -d 2>/dev/null)" || work="$STATE_DIR/.autoinstall-work.$$"
  mkdir -p "$work" 2>/dev/null
  trap 'rm -rf "$work" 2>/dev/null' RETURN
  today_used="$(installs_today)"
  case "$today_used" in ''|*[!0-9]*) today_used=0 ;; esac

  # Appends to do_run's blocked/newly_blocked via bash dynamic scoping. The
  # -d re-check swallows the hook/sweep race: if the other layer installed and
  # archived this draft mid-loop, a gate "failure" against the vanished dir
  # must not surface as a phantom block notification.
  record_block() { # <draft-dir> <id> <reason>
    local newflag
    [ -d "$1" ] || return 0
    newflag="$(mark_blocked "$1" "$3")"
    blocked+=("$(jq -nc --arg id "$2" --arg reason "$3" '{id:$id, reason:$reason}')")
    [ "$newflag" = "new" ] && newly_blocked+=("$2")
    log "blocked id=$2 reason=$3 trigger=$TRIGGER"
    return 0
  }

  while IFS= read -r dir; do
    [ -d "$dir" ] || continue
    id="$(basename "$dir")"
    f="$dir/SKILL.md"
    if ! verdict="$(gate_lint "$f")"; then
      record_block "$dir" "$id" "$verdict"; continue
    fi
    name="$(fm_field "$f" name)"
    desc="$(fm_field "$f" description)"
    if ! verdict="$(gate_secrets "$f")"; then
      record_block "$dir" "$id" "$verdict"; continue
    fi
    if ! verdict="$(gate_node_specific "$f")"; then
      record_block "$dir" "$id" "$verdict"; continue
    fi
    if ! verdict="$(gate_dedup "$name" "$desc" "$work")"; then
      record_block "$dir" "$id" "$verdict"; continue
    fi

    if [ $((today_used + ${#installed[@]})) -ge "$DAILY_CAP" ]; then
      deferred=$((deferred + 1))
      log "deferred id=$id reason=daily-cap cap=$DAILY_CAP trigger=$TRIGGER"
      continue
    fi

    # Install: narrow write surface — only $SKILLS_DIR/<kebab-name>/.
    dest="$SKILLS_DIR/$name"
    sid="$(jq -r '.session_id // empty' "$dir/meta.json" 2>/dev/null)"
    if ! mkdir -p "$dest" 2>/dev/null \
       || ! cp "$f" "$dest/SKILL.md" 2>/dev/null; then
      log "install failed id=$id name=$name reason=write-error trigger=$TRIGGER"
      continue
    fi
    sha="$(file_sha "$dest/SKILL.md")"
    rec="$(jq -nc --arg ts "$(ts)" --arg id "$id" --arg name "$name" \
      --arg path "$dest/SKILL.md" --arg sid "$sid" --arg sha "$sha" --arg trg "$TRIGGER" \
      '{event:"install", ts:$ts, id:$id, name:$name, path:$path,
        session_id:$sid, sha256:$sha, installed_by:"autosave", trigger:$trg}')"
    printf '%s\n' "$rec" >> "$LEDGER" 2>/dev/null || true
    printf '%s\n' "$rec" > "$dest/.autosave-meta.json" 2>/dev/null || true
    if [ -f "$dir/meta.json" ]; then
      jq --arg at "$(ts)" '.status="installed" | .installed_by="autosave" | .installed_at=$at' \
        "$dir/meta.json" > "$dir/meta.json.tmp" 2>/dev/null \
        && mv "$dir/meta.json.tmp" "$dir/meta.json" 2>/dev/null || rm -f "$dir/meta.json.tmp"
    fi
    rm -f "$dir/autosave-block.json" 2>/dev/null
    mv "$dir" "$dir.installed-$(ts_id)" 2>/dev/null || true
    installed+=("$name")
    log "installed id=$id name=$name sha=$sha trigger=$TRIGGER"
  done < <(pending_drafts)

  summary="$(jq -nc \
    --argjson installed "$(printf '%s\n' "${installed[@]:-}" | jq -R . | jq -sc 'map(select(length>0))')" \
    --argjson blocked "$(printf '%s\n' "${blocked[@]:-}" | jq -sc 'map(select(type=="object"))' 2>/dev/null || printf '[]')" \
    --argjson newly_blocked "$(printf '%s\n' "${newly_blocked[@]:-}" | jq -R . | jq -sc 'map(select(length>0))')" \
    --argjson deferred "$deferred" \
    --argjson pending "$(pending_count)" \
    '{mode:"auto", installed:$installed, blocked:$blocked,
      newly_blocked:$newly_blocked, deferred:$deferred, pending:$pending}')"
  printf '%s\n' "$summary"
  log "run done $summary"
  notify_summary "$summary"
  return 0
}

# After-the-fact owner notification — same spool contract as the sweep/notify.sh:
# short redaction-safe text; the bridge PushNotifier (opt-in) delivers it.
notify_summary() { # <summary-json>
  local summary="$1" n_inst n_new_blk n_def text parts dedup now fname node
  [ "$NOTIFY" = "1" ] || return 0
  n_inst="$(jq -r '.installed | length' <<<"$summary" 2>/dev/null || printf 0)"
  n_new_blk="$(jq -r '.newly_blocked | length' <<<"$summary" 2>/dev/null || printf 0)"
  n_def="$(jq -r '.deferred' <<<"$summary" 2>/dev/null || printf 0)"
  [ "$n_inst" -gt 0 ] 2>/dev/null || [ "$n_new_blk" -gt 0 ] 2>/dev/null || return 0
  parts=""
  if [ "$n_inst" -gt 0 ]; then
    parts="스킬 자동 설치 ${n_inst}건: $(jq -r '.installed | join(", ")' <<<"$summary")"
  fi
  if [ "$n_new_blk" -gt 0 ]; then
    [ -n "$parts" ] && parts="$parts · "
    parts="${parts}자동 설치 차단 ${n_new_blk}건($(jq -r \
      '.newly_blocked as $ni | [.blocked[] | select(.id as $i | $ni | index($i))
       | (.reason | split(" ")[0])] | unique | join(",")' <<<"$summary" 2>/dev/null)) — 승인 대기 유지"
  fi
  if [ "$n_def" -gt 0 ] 2>/dev/null; then
    parts="$parts · 일일 상한(${DAILY_CAP}건) 도달로 ${n_def}건 보류"
  fi
  text="$parts — '/skill-suggest'로 사후 검토/롤백하세요."
  mkdir -p "$SPOOL" 2>/dev/null || return 0
  node="${CCC_NODE:-$(hostname -s 2>/dev/null || echo node)}"
  now="$(ts)"
  dedup="SkillAutoInstall:$(jq -r '.installed | join(",")' <<<"$summary"):$(jq -r '.newly_blocked | join(",")' <<<"$summary")"
  dedup="$(printf '%s' "$dedup" | cut -c1-120)"
  fname="$SPOOL/$(printf '%s' "$now" | tr ':' '-')-SkillAutoInstall-$$.json"
  if jq -nc --arg ts "$now" --arg node "$node" --arg text "$text" --arg dedup "$dedup" \
      '{ts:$ts, event:"SkillAutoInstall", node:$node, text:$text, dedup:$dedup}' \
      > "$fname" 2>/dev/null; then
    log "notify queued spool=$fname"
  else
    rm -f "$fname" 2>/dev/null
    log "notify failed (non-fatal)"
  fi
}

# ---------------------------------------------------------------------------
# rollback / list / status
# ---------------------------------------------------------------------------

rollback_one() { # <name>
  local name="$1" dir arch
  printf '%s' "$name" | grep -qE "$KEBAB" || { echo "rollback: invalid name: $name" >&2; return 1; }
  dir="$SKILLS_DIR/$name"
  if [ ! -f "$dir/.autosave-meta.json" ]; then
    echo "rollback: $name is not autosave-installed (no .autosave-meta.json) — refusing" >&2
    return 1
  fi
  mkdir -p "$ROLLBACK_DIR" 2>/dev/null
  arch="$ROLLBACK_DIR/$name.$(ts_id)"
  mv "$dir" "$arch" 2>/dev/null || { echo "rollback: failed to archive $dir" >&2; return 1; }
  jq -nc --arg ts "$(ts)" --arg name "$name" --arg arch "$arch" \
    '{event:"rollback", ts:$ts, name:$name, archived_to:$arch}' >> "$LEDGER" 2>/dev/null || true
  log "rollback name=$name archived_to=$arch"
  echo "rolled back: $name -> $arch"
  return 0
}

do_rollback() {
  local target="${1:-}" name rc=0
  [ -n "$target" ] || { echo "usage: autoinstall.sh rollback <name>|--all" >&2; return 2; }
  if [ "$target" = "--all" ]; then
    # Marker-driven, not ledger-driven: the in-dir marker is the proof of an
    # autosave install, so bulk rollback still works if the ledger was pruned.
    local any=0 m
    while IFS= read -r m; do
      any=1
      name="$(basename "$(dirname "$m")")"
      rollback_one "$name" || rc=1
    done < <(find "$SKILLS_DIR" -mindepth 2 -maxdepth 2 -name .autosave-meta.json 2>/dev/null | sort)
    [ "$any" = 1 ] || echo "rollback: no autosave-installed skills found"
    return "$rc"
  fi
  rollback_one "$target"
}

do_list() {
  echo "mode: $(resolve_mode) (env CCC_SKILL_AUTOSAVE_MODE > $MODE_FILE > approve)"
  echo "== autosave install ledger ($LEDGER) =="
  if [ -s "$LEDGER" ]; then
    jq -r '[.ts, .event, .name] | @tsv' "$LEDGER" 2>/dev/null | sed 's/^/  /'
  else
    echo "  (empty)"
  fi
  echo "== currently installed by autosave =="
  local found=0 m
  while IFS= read -r m; do
    found=1
    printf '  %s (%s)\n' "$(basename "$(dirname "$m")")" "$(jq -r '.ts // "?"' "$m" 2>/dev/null)"
  done < <(find "$SKILLS_DIR" -mindepth 2 -maxdepth 2 -name .autosave-meta.json 2>/dev/null | sort)
  [ "$found" = 1 ] || echo "  (none)"
  echo "== blocked pending drafts (stay human-reviewed) =="
  found=0
  local dir
  while IFS= read -r dir; do
    [ -f "$dir/autosave-block.json" ] || continue
    found=1
    printf '  %s reason=%s\n' "$(basename "$dir")" \
      "$(jq -r '.reason // "?"' "$dir/autosave-block.json" 2>/dev/null)"
  done < <(pending_drafts)
  [ "$found" = 1 ] || echo "  (none)"
}

do_status() {
  echo "mode: $(resolve_mode)"
  echo "off-switch: $([ -f "$STATE_DIR/skill-autosave.disabled" ] && echo ON || echo off)"
  echo "daily cap: $(installs_today)/$DAILY_CAP used today"
  echo "pending drafts: $(pending_count)"
  echo "-- install ledger (last 5) --"; tail -5 "$LEDGER" 2>/dev/null || true
  echo "-- log (last 5) --"; tail -5 "$LOG" 2>/dev/null || true
}

MODE_VERB="${1:-run}"
case "$MODE_VERB" in
  run) do_run ;;
  list) do_list ;;
  rollback) shift; do_rollback "${1:-}" ;;
  status) do_status ;;
  *) echo "usage: autoinstall.sh [run|list|rollback <name>|--all|status]" >&2; exit 2 ;;
esac
