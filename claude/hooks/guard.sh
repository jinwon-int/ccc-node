#!/usr/bin/env bash
# PreToolUse guard — fail-closed enforcement of the "Fresh Approval Required" boundary.
#
# Reads the PreToolUse hook payload on stdin ({tool_name, tool_input:{command|file_path}}).
# Exit 0 = allow; exit 2 = deny (the harness aborts the tool call and shows stderr to Claude).
#
# Policy: separation of approval from execution. Gated actions are DENIED by default and
# require an explicit operator approval signal — set CCC_ALLOW_GATED=1 in the environment
# only after the operator has approved the specific action (this is the bypass-by-operator).
#
# Risk-profile model (see RISK-PROFILES.md):
#   autonomous              — not matched here; proceeds silently.
#   operator_notify         — proceeds; captured by the PostToolUse audit log (audit.sh).
#   operator_approval_gated — DENIED until CCC_ALLOW_GATED=1 (operator approves the action).
#   operator_review_gated   — DENIED; history/published-state change needing review evidence too.
# guard.sh enforces the two *gated* profiles (deny). The other two are non-blocking.
#
# Design notes:
#   - No `set -e`: grep "no match" returns 1 and must not abort the script.
#   - Fail-OPEN only if jq/stdin is unavailable (jq is a harness dependency); everything else
#     fails CLOSED. Patterns favor precision to avoid blocking normal git/gh/npm/file work.
set -uo pipefail

input="$(cat 2>/dev/null)"
[ -n "$input" ] || exit 0

tool="$(printf '%s' "$input"  | jq -r '.tool_name // empty'          2>/dev/null)"
cmd="$(printf '%s' "$input"   | jq -r '.tool_input.command // empty' 2>/dev/null)"
fpath="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"

# deny <label> <profile> <detail>
deny() {
  local label="$1" profile="$2" detail="$3"
  # Observability: record the denial (risk label + profile + tool only — never the raw
  # command, which may carry secrets) so blocked gated actions surface as approval-needed.
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
  local approval_log="${CCC_APPROVAL_LOG:-${HOME:-/root}/.claude/state/approval-needed.log}"
  mkdir -p "$(dirname "$approval_log")" 2>/dev/null
  printf '%s\tDENY[%s]\tprofile=%s\ttool=%s\n' "$ts" "$label" "$profile" "${tool:-?}" >> "$approval_log" 2>/dev/null
  echo "BLOCKED by ccc-node guard [$label] (profile=$profile): ${detail}" >&2
  echo "→ Fresh Approval Required (CLAUDE.md). After the operator approves THIS action, re-run with CCC_ALLOW_GATED=1." >&2
  exit 2
}

# --- Operator escape hatch: explicit, audited approval signal ---
if [ "${CCC_ALLOW_GATED:-0}" = "1" ]; then
  echo "ccc-node guard: CCC_ALLOW_GATED=1 set — gated action allowed by operator (audit: tool=$tool)." >&2
  exit 0
fi

# --- Secret-file access via Read/Edit/Write tools (path-based) ---
case "$tool" in
  Read|Edit|Write|NotebookEdit|MultiEdit)
    case "$fpath" in
      *.template.*|*.env.example|*.env.template|*.env.sample) : ;;  # templates/examples are safe
      */.env|*/.env.*|*.env|*.credentials.json|*.pem|*/id_rsa|*/id_rsa.*|*.key)
        # Covers .env, .env.local, .env.production, foo.env, etc. (templates carved above).
        deny "secret-file" "operator_approval_gated" "$tool on $fpath" ;;
    esac
    ;;
esac

# --- Bash command-content patterns ---
[ "$tool" = "Bash" ] || exit 0
[ -n "$cmd" ] || exit 0
c="$cmd"

g() { grep -Eq "$1" <<<"$c"; }   # case-sensitive
gi() { grep -Eiq "$1" <<<"$c"; } # case-insensitive

# Quote-stripped view: dangerous tokens must not be hidden behind quotes, e.g.
# `rm -rf "/root"` or `cat ".env"`. Matching the de-quoted form only ever ADDS
# denials (never removes them), so it cannot loosen the guard.
cn="${c//\"/}"; cn="${cn//\'/}"
gn() { grep -Eq "$1" <<<"$cn"; }    # case-sensitive, quote-stripped

# 0 = safe low-risk local Telegram bridge restart.
ccc_telegram_bridge_restart() {
  # Never allow the carve-out to hide chained/compound service controls.
  case "$c" in *';'*|*'&'*|*'|'*|*'`'*|*'$('*|*$'\n'*) return 1;; esac
  local toks; read -ra toks <<<"$c"
  local n=${#toks[@]} i=0 si=-1 service_cmds=0
  while [ "$i" -lt "$n" ]; do
    case "${toks[$i]}" in
      systemctl|service) service_cmds=$((service_cmds+1)); [ "$si" -lt 0 ] && si=$i ;;
    esac
    i=$((i+1))
  done
  [ "$service_cmds" -eq 1 ] || return 1

  [ "$((si + 2))" -lt "$n" ] || return 1
  [ "${toks[$((si + 1))]}" = "restart" ] || return 1
  case "${toks[$((si + 2))]}" in
    ccc-telegram-bridge|ccc-telegram-bridge.service) : ;;
    *) return 1 ;;
  esac
  [ "$((si + 3))" -eq "$n" ] || return 1
  return 0
}

# force push / history rewrite
#
# Force-push is review-gated by default, BUT auto-allowed (operator-approved
# relaxation) for an *explicit single* force-push to a NON-protected feature
# branch. Protected branches (main/master/develop/release*/…), ambiguous or
# bare targets (no explicit dst, HEAD, current branch), multiple refspecs, and
# compound/chained commands ALL stay DENIED — fail-closed when uncertain.
# 0 = the command's git subcommand is `push`, tolerating global options that sit
# between `git` and the subcommand (e.g. `git -C <dir> push`, `git -c k=v push`).
# The old regex required `git` and `push` to be adjacent, so `git -C x push
# --force origin main` slipped past the review gate entirely.
cmd_is_git_push() {
  local toks; read -ra toks <<<"$c"
  local n=${#toks[@]} i=0
  while [ "$i" -lt "$n" ] && [ "${toks[$i]}" != "git" ]; do i=$((i+1)); done
  [ "$i" -lt "$n" ] || return 1
  i=$((i+1))
  while [ "$i" -lt "$n" ]; do
    case "${toks[$i]}" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix) i=$((i+2)) ;;  # opt + value
      push) return 0 ;;
      -*) i=$((i+1)) ;;        # value-less global flag
      *) return 1 ;;           # first non-option subcommand isn't push
    esac
  done
  return 1
}

is_forcepush() {
  cmd_is_git_push || return 1
  g '([[:space:]]-[a-zA-Z]*f\b|--force-with-lease|--force([[:space:]=]|$))' && return 0
  g '[[:space:]]\+[A-Za-z0-9_./-]+:'                                        && return 0  # +src:dst
  g '[[:space:]]\+[A-Za-z0-9_./-]+([[:space:]]|$)'                          && return 0  # +branch
  return 1
}

# 0 = safe (single explicit force-push to a clear non-protected feature branch).
forcepush_to_feature_branch() {
  # Never safe if the command chains/embeds anything else.
  case "$c" in *';'*|*'&'*|*'|'*|*'`'*|*'$('*|*$'\n'*) return 1;; esac
  # Exactly one `git push` invocation.
  [ "$(grep -oE 'git[[:space:]]+push\b' <<<"$c" | wc -l)" -eq 1 ] || return 1

  local toks; read -ra toks <<<"$c"
  local n=${#toks[@]} i=0 pi=-1
  while [ "$i" -lt "$n" ]; do [ "${toks[$i]}" = "push" ] && { pi=$i; break; }; i=$((i+1)); done
  [ "$pi" -ge 0 ] || return 1

  # Collect positional args after `push`, skipping flags (and value-taking flags).
  local positionals=() j=$((pi+1)) t
  while [ "$j" -lt "$n" ]; do
    t="${toks[$j]}"
    case "$t" in
      -o|--push-option|--repo|--exec|--receive-pack) j=$((j+2)); continue ;;  # flag + its value
      -*) j=$((j+1)); continue ;;                                             # other flags
      *) positionals+=("$t") ;;
    esac
    j=$((j+1))
  done

  # Require exactly: <remote> <single-refspec>. Anything else is ambiguous/multi.
  [ "${#positionals[@]}" -eq 2 ] || return 1
  local refspec="${positionals[1]}"
  refspec="${refspec#+}"          # drop leading + (force refspec)
  local dst="${refspec##*:}"      # dst = after last ':' (whole token if no ':')
  [ -n "$dst" ] || return 1

  # Protected / ambiguous destinations stay gated.
  case "$dst" in
    main|master|develop|HEAD|@|prod|production|stable) return 1 ;;
    release|release/*|release-*|releases/*|hotfix/*)    return 1 ;;
  esac
  # dst must be a plain branch ref (no globs / refspec tricks).
  printf '%s' "$dst" | grep -Eq '^[A-Za-z0-9._/-]+$' || return 1
  return 0
}

if is_forcepush; then
  forcepush_to_feature_branch \
    || deny "force-push" "operator_review_gated" "$c"
  # else: operator-approved relaxation — single force-push to a non-protected feature branch.
fi
g 'git[[:space:]]+(filter-branch|filter-repo)([[:space:]]|$)|git-filter-repo'                               && deny "history-rewrite" "operator_review_gated" "$c"

# broker / Gateway / worker service control (operator-gated, fleet-risky).
# NOTE: ccc-telegram-bridge restart is intentionally NOT gated — it is a local,
# single-node Telegram channel restart (low blast radius), unlike broker/Gateway/
# worker restarts which can disrupt the A2A fleet mid-task. The carve-out below
# still fast-allows the bare local form; dropping `bridge` from the deny patterns
# additionally permits remote (ssh-wrapped) ccc-telegram-bridge restarts used by
# fleet rollouts, without loosening any broker/Gateway/worker/DB/secret gate.
ccc_telegram_bridge_restart && exit 0
g '(systemctl|service|supervisorctl|pm2)[[:space:]]+(restart|stop|start|reload|kill)([[:space:]]).*(broker|gateway|worker|a2a|hermes|openclaw)' && deny "service-control" "operator_approval_gated" "$c"
gi '\b(restart|reload)[-_](broker|gateway|worker)\b' && deny "service-control" "operator_approval_gated" "$c"

# DB destructive / migration / replay
gi '\b(DROP[[:space:]]+(TABLE|DATABASE)|TRUNCATE[[:space:]]|FLUSHALL|FLUSHDB)\b'                  && deny "db-destructive" "operator_approval_gated" "$c"
gi '\b(db:migrate|prisma[[:space:]]+migrate[[:space:]]+(deploy|dev)|alembic[[:space:]]+(upgrade|downgrade)|knex[[:space:]]+migrate)\b' && deny "db-migrate" "operator_approval_gated" "$c"
g '[[:space:]]replay([[:space:]]|$)'                                                              && deny "replay" "operator_approval_gated" "$c"

# release / publish / tag-push / repo visibility
g 'npm[[:space:]]+publish([[:space:]]|$)|gh[[:space:]]+release[[:space:]]+create([[:space:]]|$)|git[[:space:]]+push([[:space:]]|$)[^|;&]*--tags' && deny "release/publish" "operator_review_gated" "$c"
g 'gh[[:space:]]+repo[[:space:]]+edit([[:space:]]|$)[^|;&]*--visibility'                          && deny "repo-visibility" "operator_approval_gated" "$c"

# secret read / exfil (quote-stripped; `.env` matched only when NOT followed by
# more name chars, so `.env` is caught but `.env.example` templates are not).
# Verb list extended beyond pagers to copy/encode tools (cp/mv/dd/tee/base64/…).
gn '\b(cat|less|more|head|tail|xxd|od|strings|bat|nl|tac|cp|mv|dd|tee|install|rsync|base64|gpg|openssl)\b[^|;&]*(\.env([^A-Za-z0-9_.-]|$)|\.credentials\.json|\bid_rsa\b|\.pem([[:space:]]|$))' && deny "secret-read" "operator_approval_gated" "$c"
# Indirect read via an interpreter, e.g. python3 -c "open('.env').read()".
gn '\b(python3?|ruby|perl|node|php)\b[^|;&]*(\.env([^A-Za-z0-9_.-]|$)|\.credentials|\bid_rsa\b|\.pem\b)' && deny "secret-indirect-read" "operator_approval_gated" "$c"
gn '\b(curl|wget|nc|ncat|scp|sftp|ftp|rsync|ssh)\b[^|;&]*(\.env([^A-Za-z0-9_.-]|$)|\.credentials|\bid_rsa\b|secret|token)' && deny "secret-exfil" "operator_approval_gated" "$c"

# catastrophic rm against absolute / home roots (quote-stripped; long flags too)
gn '\brm\b([[:space:]]+--?[A-Za-z-]+)*[[:space:]]+(/|~|\$HOME|/root|/etc|/var|/usr|/bin|/lib)([[:space:]/]|$)' && deny "rm-catastrophic" "operator_approval_gated" "$c"

exit 0
