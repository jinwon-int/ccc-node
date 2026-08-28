#!/usr/bin/env bash
# skill-review/autoinstall.sh — Hermes-style unattended skill installation (#355).
#
# In approve mode (the default) this script is a no-op: drafts staged under
# ~/.claude/state/pending-skills/ wait for a human (/skillsuggest). In auto
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
#   ownership-status    ownership/pin/autonomous-write classification (JSON)
#   list-unmanaged      protected/non-autonomous skills (JSON)
#   adopt|pin|unpin     explicit owner controls (support --dry-run)
#   render <draft-id>   render one v2 action/target/diff for owner review
#   apply <draft-id>    explicitly apply one reviewed v2 proposal
set -uo pipefail
export LC_ALL=C

CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
# Anchored to the node-global Claude dir on purpose — NOT to CCC_STATE_DIR.
# The bridge exports CCC_STATE_DIR per memory audience (memory_audience.py,
# hook_environment), so honouring it here pointed the queue at a per-audience
# memory scope while the collector kept staging drafts into the node-global
# ~/.claude/state/pending-skills (settings_memory.py codex_skill_pending_dir).
# Skills install to one node-wide skills dir, so the queue is node-global too.
STATE_DIR="${CCC_SKILL_REVIEW_STATE_DIR:-$CLAUDE_DIR/state}"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$CLAUDE_DIR/skills}"
PENDING_DIR="${CCC_SKILL_REVIEW_PENDING_DIR:-$STATE_DIR/pending-skills}"
SPOOL="${CCC_PUSH_SPOOL:-$STATE_DIR/telegram-spool}"
LOG="$STATE_DIR/skill-autoinstall.log"
LEDGER="$STATE_DIR/skill-autosave-install.jsonl"
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
# shellcheck source=claude/hooks/skill-review/provider.sh
. "$AUTOINSTALL_LIB_DIR/provider.sh" 2>/dev/null || true
# shellcheck source=claude/hooks/lib/autonomy-guard.sh
. "$AUTOINSTALL_LIB_DIR/../lib/autonomy-guard.sh" 2>/dev/null || true
OWNERSHIP_TOOL="$AUTOINSTALL_LIB_DIR/ownership.py"
ts_id() { date -u +%Y%m%d%H%M%S; }

# Provider-neutral install target (#643): the gate/ledger/rollback pipeline is
# identical across providers; only the skills directory and a compatibility
# screen differ. Falls back to the historical Claude default if provider.sh is
# somehow unavailable, so existing Claude nodes are unchanged.
if declare -f ccc_skill_provider >/dev/null 2>&1; then
  SKILL_PROVIDER="$(ccc_skill_provider)"
  SKILLS_DIR="$(ccc_skills_dir "$SKILL_PROVIDER")"
else
  SKILL_PROVIDER="claude"
fi

ownership_cmd() {
  command -v python3 >/dev/null 2>&1 || {
    printf '{"ok":false,"code":"python3_missing"}\n' >&2
    return 2
  }
  [ -r "$OWNERSHIP_TOOL" ] || {
    printf '{"ok":false,"code":"ownership_tool_missing"}\n' >&2
    return 2
  }
  python3 "$OWNERSHIP_TOOL" \
    --provider "$SKILL_PROVIDER" \
    --skills-dir "$SKILLS_DIR" \
    --state-dir "$STATE_DIR" \
    "$@"
}

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
    | grep -Ev '/\.stage-|(\.(approved|rejected|installed)-[0-9]+$)' | sort
}

pending_count() { pending_drafts | grep -c . ; }

installs_today() {
  jq -r 'select(.event=="install") | .ts' "$LEDGER" 2>/dev/null | grep -c "^$(date -u +%F)"
}

incremental_installs_today() {
  local value day
  if value="$(ownership_cmd automatic-usage 2>/dev/null | jq -r '.used // empty' 2>/dev/null)"; then
    case "$value" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$value"
    return 0
  fi
  day="$(date -u +%F)"
  if [ ! -e "$STATE_DIR/skill-autosave-ownership.jsonl" ]; then
    printf '0\n'
    return 0
  fi
  value="$(jq -s -r --arg day "$day" '
    reduce (
      .[]
      | select(
          .event == "skill-proposal-apply"
          and .automatic == true
          and .cap_day == $day
          and (.proposal_id | type) == "string"
          and (.outcome | type) == "string"
        )
    ) as $row ({}; .[$row.proposal_id] = $row.outcome)
    | [.[] | select(. == "prepared" or . == "applied")] | length
  ' "$STATE_DIR/skill-autosave-ownership.jsonl" 2>/dev/null)" || return 1
  case "$value" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$value"
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
    'api-key::(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}'
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

gate_codex_compat() { # <skill.md> — non-Claude providers reject Claude-only couplings
  # No-op for the Claude provider. On a Codex or Piri install target a draft
  # that hard-codes the Claude CLI, the ~/.claude tree, or CLAUDE_* env can't
  # run, so it stays human-reviewed (pending) instead of installing. Prose
  # that merely mentions "Claude Code" is untouched — only concrete path/CLI/env
  # couplings match, and the reason is a label only so markers/logs stay
  # redaction-safe. Reason labels keep the historical codex-incompat prefix.
  local f="$1"
  case "${SKILL_PROVIDER:-claude}" in
    codex|piri) ;;
    *) return 0 ;;
  esac
  if grep -qE '(^|[^A-Za-z0-9_-])claude[[:space:]]+-p([[:space:]]|$)' "$f" 2>/dev/null; then
    printf 'codex-incompat claude-cli'; return 1
  fi
  if grep -qE '(^|[^A-Za-z0-9_])(~|\$HOME|\$\{HOME[^}]*\})?/?\.claude/' "$f" 2>/dev/null; then
    printf 'codex-incompat claude-home'; return 1
  fi
  if grep -qE 'CLAUDE_(SKILLS_DIR|PROJECTS_DIR|CLI_PATH|CONFIG|CODE)' "$f" 2>/dev/null; then
    printf 'codex-incompat claude-env'; return 1
  fi
  return 0
}

gate_unverified_claims() { # <skill.md> — falsifiable assertions must carry a citation
  # Rationale (#1307 follow-up): the machine cannot check whether a claim is
  # TRUE, but it can check whether the claim is CITED. Two auto-installed
  # drafts on 2026-08-27 asserted a `gh` exit code that does not exist and a
  # docs URL that 404s; both were uncheckable by the reader as written. This
  # gate is therefore about citability, not truth: a draft that states a
  # falsifiable technical fact with no way to re-derive it stays pending for
  # human review. Reasons are pattern labels only — draft content is never
  # quoted, so logs/markers/notifications stay redaction-safe.
  local f="$1" risky="" url code checked=0
  [ "${CCC_SKILL_GATE_CLAIMS:-1}" = 1 ] || return 0

  # --- risky claim families -------------------------------------------------
  if grep -qiE '(exits?|exited|exit[[:space:]]+(code|status))[[:space:]]+(with[[:space:]]+)?(code[[:space:]]+)?[0-9]{1,3}([^0-9]|$)' "$f" 2>/dev/null; then
    risky="exit-code"
  elif grep -qiE '(HTTP|status[[:space:]]+code)[[:space:]]+[0-9]{3}([^0-9]|$)' "$f" 2>/dev/null; then
    risky="http-status"
  elif grep -qiE '(^|[^a-z0-9_.-])v?[0-9]+\.[0-9]+\.[0-9]+([^0-9]|$)' "$f" 2>/dev/null; then
    risky="version-pin"
  fi

  if [ -n "$risky" ]; then
    # --- evidence markers that make the claim re-derivable -------------------
    if grep -qE 'https?://' "$f" 2>/dev/null; then :
    elif grep -qE '\.(go|py|ts|tsx|js|jsx|rs|sh|rb|java|cc?|cpp|hp?p?|ya?ml|json|toml):[0-9]+' "$f" 2>/dev/null; then :
    elif grep -qE '(--help|[[:space:]]help[[:space:]]|--version)' "$f" 2>/dev/null; then :
    elif grep -qiE '(verified|confirmed|measured|observed|검증|확인)[^\n]{0,40}20[0-9]{2}-[0-9]{2}-[0-9]{2}' "$f" 2>/dev/null; then :
    else
      printf 'unverified-claim %s' "$risky"; return 1
    fi
  fi

  # --- dead citations -------------------------------------------------------
  # Fail-OPEN on network trouble (cron may run offline); block only on a
  # definitive 404/410, which means the citation cannot be checked by anyone.
  [ "${CCC_SKILL_GATE_URLCHECK:-1}" = 1 ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  while IFS= read -r url; do
    [ "$checked" -lt "${CCC_SKILL_GATE_URLMAX:-6}" ] || break
    checked=$((checked + 1))
    if url_missing "$url"; then printf 'dead-citation http-404'; return 1; fi
  done < <(claim_urls "$f")
  return 0
}

url_missing() { # <url> — 0 only when the URL is DEFINITIVELY gone
  # Fail-open everywhere else. Two classes of 404 are not dead citations:
  #   * template placeholders (OWNER/REPO/NUM/<sha>) — 404 by construction
  #   * private GitHub resources — indistinguishable from deleted ones without
  #     auth, so re-check with `gh` before condemning them
  local u="$1" code slug api
  case "$u" in
    *OWNER*|*REPO*|*NUM*|*SHA*|*BRANCH*|*PR_*|*_ID*|*'<'*|*'{'*|*example.com*|*path/to/*|*YOUR*|*xxx*)
      return 1 ;;
  esac
  # Some doc hosts (support.google.com, nvd.nist.gov) 403/404 curl's default
  # UA, so a browser UA is sent and a HEAD-hostile host is retried with GET.
  local UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
  code="$(curl -sIL -A "$UA" -o /dev/null -w '%{http_code}' --max-time 8 "$u" 2>/dev/null)" || return 1
  case "$code" in
    404|410)
      code="$(curl -sL -A "$UA" -o /dev/null -w '%{http_code}' --max-time 10 "$u" 2>/dev/null)" || return 1
      case "$code" in 404|410) ;; *) return 1 ;; esac
      ;;
    *) return 1 ;;
  esac
  if printf '%s' "$u" | grep -qE '^https?://(www\.)?github\.com/'; then
    command -v gh >/dev/null 2>&1 || return 1
    slug="$(printf '%s' "$u" | sed -E 's#^https?://(www\.)?github\.com/##; s#/$##')"
    api="$(printf '%s' "$slug" \
      | sed -E 's#^([^/]+/[^/]+)/pull/([0-9]+)$#repos/\1/pulls/\2#
                s#^([^/]+/[^/]+)/issues/([0-9]+)$#repos/\1/issues/\2#
                s#^([^/]+/[^/]+)$#repos/\1#')"
    case "$api" in
      repos/*) gh api "$api" >/dev/null 2>&1 && return 1 ;;
      *) return 1 ;;
    esac
  fi
  return 0
}

claim_urls() { # <skill.md> — every citable URL, deduped, scheme-normalized
  # Schemeless citations ("docs.github.com/en/rest/pulls/") are the common form
  # in prose and were missed by a scheme-anchored match, so they are collected
  # too and probed over https. Requires host.tld/ shape, which excludes source
  # refs like checks.go:303 and relative paths like docs/skill-autosave.md.
  # A line that documents a URL as broken ("...this form 404s — use X instead")
  # is a warning to the reader, not a citation, so those lines are excluded
  # before extraction. Otherwise a skill can never record a known-dead path.
  local f="$1" body
  # Note the trailing [a-z]* — "404s"/"410s" read naturally in prose and a
  # plain \b404\b silently fails to match them.
  body="$(grep -viE '(^|[^0-9])(404|410)[a-z]*([^0-9]|$)|dead[- ]?(link|citation)|no longer (exists|valid|works)|deprecated|obsolete' "$f" 2>/dev/null)"
  {
    printf '%s\n' "$body" | grep -oE 'https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+'
    printf '%s\n' "$body" | grep -oE '(^|[^A-Za-z0-9._/@:-])([A-Za-z0-9-]+\.)+[A-Za-z]{2,24}/[A-Za-z0-9._~:/?#@!$&*+,;=%-]*' \
      | sed -E 's#^[^A-Za-z0-9]*##; s#^#https://#'
  } 2>/dev/null | sed -E 's#[.,)`"'"'"']+$##' \
    | grep -E '^https?://[^/]+\.[A-Za-z]{2,24}(/|$)' | sort -u
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

  # Fleet-wide autonomy guard (#386): a single kill-switch/dry-run above this
  # layer's own mode. kill => install nothing; dry-run => gate + report what
  # would install, write nothing.
  # autonomy-guard.sh resolves its own dir from CCC_STATE_DIR, so pin it to the
  # queue's anchor (the distill.sh idiom). Without this the operator kill-switch
  # at ~/.claude/state/autonomy.kill would be invisible whenever the bridge has
  # scoped CCC_STATE_DIR to a memory audience — the guard must not fail open.
  local AUTONOMY_STATE="active" AUTONOMY_DRY=0
  if declare -f ccc_autonomy_state >/dev/null 2>&1; then
    AUTONOMY_STATE="$(CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_state 2>/dev/null || echo active)"
  fi
  if [ "$AUTONOMY_STATE" = "kill" ]; then
    log "skip reason=autonomy-kill trigger=$TRIGGER"
    declare -f ccc_autonomy_record >/dev/null 2>&1 \
      && CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_record autoinstall kill "$TRIGGER"
    printf '{"mode":"auto","skipped":"autonomy-kill"}\n'
    return 0
  fi
  if [ "$AUTONOMY_STATE" = "dry-run" ]; then
    AUTONOMY_DRY=1
    declare -f ccc_autonomy_record >/dev/null 2>&1 \
      && CCC_STATE_DIR="$STATE_DIR" ccc_autonomy_record autoinstall dry-run "$TRIGGER"
  fi

  # Fail closed if the install target is unsafe (symlinked leaf / non-dir).
  # For Codex the directory is created owner-only (0700); an existing regular
  # Claude skills dir is accepted untouched. #643.
  if declare -f ccc_ensure_skills_dir >/dev/null 2>&1; then
    if ! ccc_ensure_skills_dir "$SKILLS_DIR"; then
      log "skip reason=unsafe-skills-dir provider=$SKILL_PROVIDER dir=$SKILLS_DIR trigger=$TRIGGER"
      printf '{"mode":"auto","skipped":"unsafe-skills-dir","provider":"%s"}\n' "$SKILL_PROVIDER"
      return 0
    fi
  fi

  # Atomic single-runner lock (#643 concurrency safety): mkdir is atomic, so at
  # most one runner installs at a time. Concurrent runners no-op (drafts stay
  # pending and are retried), which keeps candidates/ledger/installs duplicate
  # free even when the same checkpoint is processed many times at once. A stale
  # lock (>5 min, e.g. a killed runner) is reclaimed.
  local LOCKDIR="$STATE_DIR/.autoinstall.lock" locked=0
  if mkdir "$LOCKDIR" 2>/dev/null; then
    locked=1
  elif find "$LOCKDIR" -maxdepth 0 -mmin +5 2>/dev/null | grep -q .; then
    rm -rf "$LOCKDIR" 2>/dev/null
    mkdir "$LOCKDIR" 2>/dev/null && locked=1
  fi
  if [ "$locked" != 1 ]; then
    log "skip reason=locked trigger=$TRIGGER"
    printf '{"mode":"auto","skipped":"locked"}\n'
    return 0
  fi

  local work dir id f name desc verdict rec sha sid dest proposal_file action apply_json apply_rc validation_json
  local -a installed=() blocked=() newly_blocked=() would_install=()
  local deferred=0 failed=0 today_used legacy_used incremental_used incremental_cap
  work="$(mktemp -d 2>/dev/null)" || work="$STATE_DIR/.autoinstall-work.$$"
  mkdir -p "$work" 2>/dev/null
  trap 'rm -rf "$work" 2>/dev/null; rmdir "$LOCKDIR" 2>/dev/null' RETURN
  legacy_used="$(installs_today)"
  case "$legacy_used" in ''|*[!0-9]*) legacy_used=0 ;; esac
  if ! incremental_used="$(incremental_installs_today)"; then
    log "skip reason=incremental-usage-unavailable trigger=$TRIGGER"
    printf '{"mode":"auto","skipped":"incremental-usage-unavailable"}\n'
    return 0
  fi
  today_used=$((legacy_used + incremental_used))

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
    proposal_file="$dir/proposal.json"
    f="$dir/SKILL.md"
    if [ -f "$proposal_file" ]; then
      if [ -e "$f" ]; then
        record_block "$dir" "$id" "proposal mixed-payload"; continue
      fi
      validation_json="$(ownership_cmd validate-proposal --proposal "$proposal_file" 2>/dev/null)"
      apply_rc=$?
      if [ "$apply_rc" -ne 0 ]; then
        verdict="$(jq -r '.code // "incremental_proposal_invalid"' <<<"$validation_json" 2>/dev/null)"
        record_block "$dir" "$id" "$verdict"; continue
      fi
      action="$(jq -r '.action // empty' <<<"$validation_json" 2>/dev/null)"
      case "$action" in
        create)
          f="$work/$id.SKILL.md"
          if ! jq -j '.proposal.skill_md' "$proposal_file" > "$f" 2>/dev/null; then
            record_block "$dir" "$id" "proposal invalid-create"; continue
          fi
          ;;
        patch|write_file)
          if [ $((today_used + ${#installed[@]})) -ge "$DAILY_CAP" ]; then
            deferred=$((deferred + 1))
            log "deferred id=$id reason=daily-cap cap=$DAILY_CAP trigger=$TRIGGER"
            continue
          fi
          if [ "$AUTONOMY_DRY" = 1 ]; then
            apply_json="$(ownership_cmd apply-proposal --proposal "$proposal_file" --dry-run 2>/dev/null)"
            apply_rc=$?
            if [ "$apply_rc" -ne 0 ]; then
              verdict="$(jq -r '.code // "proposal-dry-run-failed"' <<<"$apply_json" 2>/dev/null)"
              record_block "$dir" "$id" "$verdict"; continue
            fi
            name="$(jq -r '.proposal.target_skill' "$proposal_file" 2>/dev/null)"
            would_install+=("$name:$action")
            log "dry-run id=$id name=$name action=$action reason=autonomy-dry-run trigger=$TRIGGER"
            continue
          fi
          incremental_cap=$((DAILY_CAP - legacy_used))
          if [ "$incremental_cap" -lt 1 ]; then
            deferred=$((deferred + 1))
            log "deferred id=$id reason=daily-cap cap=$DAILY_CAP trigger=$TRIGGER"
            continue
          fi
          apply_json="$(ownership_cmd apply-proposal --proposal "$proposal_file" \
            --automatic --daily-cap "$incremental_cap" 2>/dev/null)"
          apply_rc=$?
          if [ "$apply_rc" -ne 0 ]; then
            verdict="$(jq -r '.code // "proposal-apply-failed"' <<<"$apply_json" 2>/dev/null)"
            if [ "$verdict" = "incremental_daily_cap_exhausted" ]; then
              deferred=$((deferred + 1))
              log "deferred id=$id reason=daily-cap cap=$DAILY_CAP trigger=$TRIGGER"
              continue
            fi
            record_block "$dir" "$id" "$verdict"; continue
          fi
          name="$(jq -r '.proposal.target_skill' "$proposal_file" 2>/dev/null)"
          if [ -f "$dir/meta.json" ]; then
            jq --arg at "$(ts)" '.status="installed" | .installed_by="autosave" | .installed_at=$at' \
              "$dir/meta.json" > "$dir/meta.json.tmp" 2>/dev/null \
              && mv "$dir/meta.json.tmp" "$dir/meta.json" 2>/dev/null || rm -f "$dir/meta.json.tmp"
          fi
          rm -f "$dir/autosave-block.json" 2>/dev/null
          mv "$dir" "$dir.installed-$(ts_id)" 2>/dev/null || true
          installed+=("$name:$action")
          log "installed id=$id name=$name action=$action trigger=$TRIGGER"
          continue
          ;;
        noop)
          record_block "$dir" "$id" "proposal staged-noop"; continue
          ;;
        *)
          record_block "$dir" "$id" "proposal invalid-action"; continue
          ;;
      esac
    fi
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
    if ! verdict="$(gate_codex_compat "$f")"; then
      record_block "$dir" "$id" "$verdict"; continue
    fi
    if ! verdict="$(gate_unverified_claims "$f")"; then
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

    # Dry-run (#386): the draft passed every gate and would install, but the
    # fleet autonomy guard is muted — report it and write nothing.
    if [ "$AUTONOMY_DRY" = 1 ]; then
      would_install+=("$name")
      log "dry-run id=$id name=$name reason=autonomy-dry-run trigger=$TRIGGER"
      continue
    fi

    # Install: narrow write surface — only $SKILLS_DIR/<kebab-name>/.
    # Perms are pinned explicitly (umask-agnostic): the ownership contract
    # fail-closes on group/other-writable skill dirs or SKILL.md (#770).
    dest="$SKILLS_DIR/$name"
    sid="$(jq -r '.session_id // empty' "$dir/meta.json" 2>/dev/null)"
    if ! mkdir -m 700 "$dest" 2>/dev/null; then
      failed=$((failed + 1))
      log "install failed id=$id name=$name reason=write-error trigger=$TRIGGER"
      continue
    fi
    if ! cp "$f" "$dest/SKILL.md" 2>/dev/null \
      || ! chmod 600 "$dest/SKILL.md" 2>/dev/null; then
      failed=$((failed + 1))
      rm -f "$dest/SKILL.md" 2>/dev/null
      rmdir "$dest" 2>/dev/null || true
      log "install failed id=$id name=$name reason=write-error trigger=$TRIGGER"
      continue
    fi
    sha="$(file_sha "$dest/SKILL.md")"
    if ! ownership_cmd mark-created "$name" >/dev/null; then
      # This directory was created by this locked install attempt and has not
      # been published as installed yet. Fail closed and leave the draft
      # pending if durable v2 provenance cannot be established.
      failed=$((failed + 1))
      rm -f "$dest/SKILL.md" "$dest/.autosave-meta.json" 2>/dev/null
      if rmdir "$dest" 2>/dev/null; then
        log "install failed id=$id name=$name reason=provenance-write cleanup=complete trigger=$TRIGGER"
      else
        log "install failed id=$id name=$name reason=provenance-write cleanup=incomplete trigger=$TRIGGER"
      fi
      continue
    fi
    rec="$(jq -nc --arg ts "$(ts)" --arg id "$id" --arg name "$name" \
      --arg path "$dest/SKILL.md" --arg sid "$sid" --arg sha "$sha" --arg trg "$TRIGGER" \
      '{event:"install", ts:$ts, id:$id, name:$name, path:$path,
        session_id:$sid, sha256:$sha, installed_by:"autosave", trigger:$trg}')"
    printf '%s\n' "$rec" >> "$LEDGER" 2>/dev/null || true
    if [ -f "$dir/meta.json" ]; then
      jq --arg at "$(ts)" '.status="installed" | .installed_by="autosave" | .installed_at=$at' \
        "$dir/meta.json" > "$dir/meta.json.tmp" 2>/dev/null \
        && mv "$dir/meta.json.tmp" "$dir/meta.json" 2>/dev/null || rm -f "$dir/meta.json.tmp"
    fi
    rm -f "$dir/autosave-block.json" 2>/dev/null
    mv "$dir" "$dir.installed-$(ts_id)" 2>/dev/null || true
    installed+=("$name")
    legacy_used=$((legacy_used + 1))
    log "installed id=$id name=$name sha=$sha trigger=$TRIGGER"
  done < <(pending_drafts)

  summary="$(jq -nc \
    --argjson installed "$(printf '%s\n' "${installed[@]:-}" | jq -R . | jq -sc 'map(select(length>0))')" \
    --argjson blocked "$(printf '%s\n' "${blocked[@]:-}" | jq -sc 'map(select(type=="object"))' 2>/dev/null || printf '[]')" \
    --argjson newly_blocked "$(printf '%s\n' "${newly_blocked[@]:-}" | jq -R . | jq -sc 'map(select(length>0))')" \
    --argjson would_install "$(printf '%s\n' "${would_install[@]:-}" | jq -R . | jq -sc 'map(select(length>0))')" \
    --argjson deferred "$deferred" \
    --argjson failed "$failed" \
    --argjson pending "$(pending_count)" \
    --arg autonomy "$AUTONOMY_STATE" \
    '{mode:"auto", autonomy:$autonomy, installed:$installed, blocked:$blocked,
      newly_blocked:$newly_blocked, would_install:$would_install,
      dry_run:($autonomy=="dry-run"), deferred:$deferred, failed:$failed, pending:$pending}')"
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
  text="$parts — '/skillsuggest'로 사후 검토/롤백하세요."
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
  local name="$1" archive_json arch
  printf '%s' "$name" | grep -qE "$KEBAB" || { echo "rollback: invalid name: $name" >&2; return 1; }
  if ! archive_json="$(ownership_cmd rollback-archive "$name")"; then
    echo "rollback: $name is not an unpinned rollback-eligible autosave install — refusing" >&2
    return 1
  fi
  arch="$(jq -r '.archive_path // empty' <<<"$archive_json" 2>/dev/null)"
  [ -n "$arch" ] || { echo "rollback: archive result missing for $name" >&2; return 1; }
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
  local legacy_used incremental_used total_used
  legacy_used="$(installs_today)"
  case "$legacy_used" in ''|*[!0-9]*) legacy_used=0 ;; esac
  if incremental_used="$(incremental_installs_today)"; then
    total_used=$((legacy_used + incremental_used))
  else
    total_used="unknown"
  fi
  echo "mode: $(resolve_mode)"
  echo "provider: ${SKILL_PROVIDER:-claude} (skills dir: $SKILLS_DIR)"
  echo "off-switch: $([ -f "$STATE_DIR/skill-autosave.disabled" ] && echo ON || echo off)"
  echo "daily cap: $total_used/$DAILY_CAP used today"
  echo "pending drafts: $(pending_count)"
  echo "-- install ledger (last 5) --"; tail -5 "$LEDGER" 2>/dev/null || true
  echo "-- log (last 5) --"; tail -5 "$LOG" 2>/dev/null || true
}

proposal_dir() { # <draft-id>
  local id="$1" dir
  printf '%s' "$id" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$' || return 2
  dir="$PENDING_DIR/$id"
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 2
  printf '%s\n' "$dir"
}

do_render() {
  local dir proposal validation rc
  dir="$(proposal_dir "${1:-}")" || {
    echo "render: invalid or missing draft id" >&2
    return 2
  }
  proposal="$dir/proposal.json"
  if [ ! -f "$proposal" ] || [ -L "$proposal" ] || [ -e "$dir/SKILL.md" ]; then
    echo "render: draft is not an isolated v2 proposal" >&2
    return 2
  fi
  validation="$(ownership_cmd validate-proposal --proposal "$proposal")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$validation"
    return "$rc"
  fi
  jq '{
    proposal_id,
    action:.proposal.action,
    name:(.proposal.name // .proposal.target_skill // null),
    relative_target:(.proposal.relative_target // null),
    expected_sha256:(.proposal.expected_sha256 // null),
    expected_provenance_revision:(.proposal.expected_provenance_revision // null),
    improvement_reason:(.proposal.improvement_reason // null),
    reason:.proposal.reason,
    evidence_excerpt:.proposal.evidence_excerpt,
    old_text:(.proposal.old_text // null),
    new_text:(.proposal.new_text // null),
    content:(.proposal.content // null),
    skill_md:(.proposal.skill_md // null),
    provenance
  }' "$proposal"
}

do_apply() {
  local dir proposal action result rc f name desc verdict dest sid sha rec lockdir validation
  dir="$(proposal_dir "${1:-}")" || {
    echo "apply: invalid or missing draft id" >&2
    return 2
  }
  proposal="$dir/proposal.json"
  if [ ! -f "$proposal" ] || [ -L "$proposal" ] || [ -e "$dir/SKILL.md" ]; then
    echo "apply: draft is not an isolated v2 proposal" >&2
    return 2
  fi
  validation="$(ownership_cmd validate-proposal --proposal "$proposal")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$validation"
    return "$rc"
  fi
  action="$(jq -r '.action // empty' <<<"$validation" 2>/dev/null)"
  case "$action" in
    patch|write_file)
      result="$(ownership_cmd apply-proposal --proposal "$proposal")"
      rc=$?
      [ "$rc" = 0 ] || {
        printf '%s\n' "$result"
        return "$rc"
      }
      ;;
    create)
      lockdir="$STATE_DIR/.autoinstall.lock"
      mkdir "$lockdir" 2>/dev/null || {
        echo '{"ok":false,"code":"autoinstall_locked"}'
        return 2
      }
      trap 'rmdir "$lockdir" 2>/dev/null' RETURN
      f="$dir/.approved-SKILL.md"
      jq -j '.proposal.skill_md' "$proposal" > "$f" 2>/dev/null || {
        rm -f "$f"
        echo '{"ok":false,"code":"incremental_create_invalid"}'
        return 2
      }
      verdict="$(gate_lint "$f")" || {
        rm -f "$f"
        jq -nc --arg code "$verdict" '{ok:false,code:$code}'
        return 2
      }
      name="$(fm_field "$f" name)"
      desc="$(fm_field "$f" description)"
      verdict="$(gate_secrets "$f")" || {
        rm -f "$f"
        jq -nc --arg code "$verdict" '{ok:false,code:$code}'
        return 2
      }
      verdict="$(gate_node_specific "$f")" || {
        rm -f "$f"
        jq -nc --arg code "$verdict" '{ok:false,code:$code}'
        return 2
      }
      verdict="$(gate_codex_compat "$f")" || {
        rm -f "$f"
        jq -nc --arg code "$verdict" '{ok:false,code:$code}'
        return 2
      }
      verdict="$(gate_unverified_claims "$f")" || {
        rm -f "$f"
        jq -nc --arg code "$verdict" '{ok:false,code:$code}'
        return 2
      }
      verdict="$(gate_dedup "$name" "$desc" "$dir")" || {
        rm -f "$f"
        jq -nc --arg code "$verdict" '{ok:false,code:$code}'
        return 2
      }
      if declare -f ccc_ensure_skills_dir >/dev/null 2>&1; then
        ccc_ensure_skills_dir "$SKILLS_DIR" || {
          rm -f "$f"
          echo '{"ok":false,"code":"unsafe-skills-dir"}'
          return 2
        }
      fi
      dest="$SKILLS_DIR/$name"
      # Pinned perms, same contract as the auto install path (#770).
      mkdir -m 700 "$dest" 2>/dev/null || {
        rm -f "$f"
        echo '{"ok":false,"code":"create-target-exists"}'
        return 2
      }
      if ! cp "$f" "$dest/SKILL.md" 2>/dev/null \
        || ! chmod 600 "$dest/SKILL.md" 2>/dev/null \
        || ! ownership_cmd mark-created "$name" >/dev/null; then
        rm -f "$dest/SKILL.md" "$dest/.autosave-meta.json" "$f" 2>/dev/null
        rmdir "$dest" 2>/dev/null || true
        echo '{"ok":false,"code":"create-publish-failed"}'
        return 2
      fi
      rm -f "$f"
      sha="$(file_sha "$dest/SKILL.md")"
      sid="$(jq -r '.provenance.source_thread_hash // empty' "$proposal" 2>/dev/null)"
      rec="$(jq -nc --arg ts "$(ts)" --arg id "$(basename "$dir")" --arg name "$name" \
        --arg path "$dest/SKILL.md" --arg sid "$sid" --arg sha "$sha" --arg trg "$TRIGGER" \
        '{event:"install",ts:$ts,id:$id,name:$name,path:$path,session_id:$sid,
          sha256:$sha,installed_by:"approved",trigger:$trg}')"
      printf '%s\n' "$rec" >> "$LEDGER" 2>/dev/null || true
      result="$(jq -nc --arg name "$name" --arg sha "$sha" \
        '{ok:true,command:"apply",action:"create",changed:true,name:$name,sha256:$sha}')"
      ;;
    *)
      echo '{"ok":false,"code":"incremental_action_invalid"}'
      return 2
      ;;
  esac
  if [ -f "$dir/meta.json" ]; then
    if jq --arg at "$(ts)" \
      '.status="approved" | .installed_by="owner-approved" | .installed_at=$at' \
      "$dir/meta.json" > "$dir/meta.json.tmp" 2>/dev/null; then
      mv "$dir/meta.json.tmp" "$dir/meta.json" 2>/dev/null \
        || rm -f "$dir/meta.json.tmp"
    else
      rm -f "$dir/meta.json.tmp"
    fi
  fi
  rm -f "$dir/autosave-block.json" 2>/dev/null
  mv "$dir" "$dir.approved-$(ts_id)" 2>/dev/null || true
  printf '%s\n' "$result"
}

MODE_VERB="${1:-run}"
case "$MODE_VERB" in
  run) do_run ;;
  list) do_list ;;
  render) shift; do_render "${1:-}" ;;
  apply) shift; do_apply "${1:-}" ;;
  rollback) shift; do_rollback "${1:-}" ;;
  status) do_status ;;
  ownership-status) shift; ownership_cmd status "$@" ;;
  list-unmanaged) shift; ownership_cmd list-unmanaged "$@" ;;
  adopt|pin|unpin) shift; ownership_cmd "$MODE_VERB" "$@" ;;
  *) echo "usage: autoinstall.sh [run|list|render <draft-id>|apply <draft-id>|rollback <name>|--all|status|ownership-status [name]|list-unmanaged|adopt <name> [--dry-run]|pin <name> [--dry-run]|unpin <name> [--dry-run]]" >&2; exit 2 ;;
esac
