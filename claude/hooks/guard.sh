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

deny() {
  echo "BLOCKED by ccc-node guard [$1]: ${2}" >&2
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
      *.template.*|*.env.example) : ;;                       # templates/examples are safe
      */.env|*.credentials.json|*.pem|*/id_rsa|*/id_rsa.*|*.key)
        deny "secret-file" "$tool on $fpath" ;;
    esac
    ;;
esac

# --- Bash command-content patterns ---
[ "$tool" = "Bash" ] || exit 0
[ -n "$cmd" ] || exit 0
c="$cmd"

g() { grep -Eq "$1" <<<"$c"; }   # case-sensitive
gi() { grep -Eiq "$1" <<<"$c"; } # case-insensitive

# force push / history rewrite
g 'git[[:space:]]+push\b.*([[:space:]]-[a-zA-Z]*f\b|--force-with-lease|--force([[:space:]=]|$))' && deny "force-push" "$c"
g 'git[[:space:]]+push\b.*[[:space:]]\+[A-Za-z0-9_./-]+:'                                         && deny "force-push-refspec" "$c"
g 'git[[:space:]]+(filter-branch|filter-repo)([[:space:]]|$)|git-filter-repo'                               && deny "history-rewrite" "$c"

# broker / Gateway / worker / bridge service control
g '(systemctl|service|supervisorctl|pm2)[[:space:]]+(restart|stop|start|reload|kill)([[:space:]]).*(broker|gateway|worker|a2a|hermes|openclaw|bridge)' && deny "service-control" "$c"
gi '\b(restart|reload)[-_](broker|gateway|bridge|worker)\b' && deny "service-control" "$c"

# DB destructive / migration / replay
gi '\b(DROP[[:space:]]+(TABLE|DATABASE)|TRUNCATE[[:space:]]|FLUSHALL|FLUSHDB)\b'                  && deny "db-destructive" "$c"
gi '\b(db:migrate|prisma[[:space:]]+migrate[[:space:]]+(deploy|dev)|alembic[[:space:]]+(upgrade|downgrade)|knex[[:space:]]+migrate)\b' && deny "db-migrate" "$c"
g '[[:space:]]replay([[:space:]]|$)'                                                              && deny "replay" "$c"

# release / publish / tag-push / repo visibility
g 'npm[[:space:]]+publish([[:space:]]|$)|gh[[:space:]]+release[[:space:]]+create([[:space:]]|$)|git[[:space:]]+push([[:space:]]|$)[^|;&]*--tags' && deny "release/publish" "$c"
g 'gh[[:space:]]+repo[[:space:]]+edit([[:space:]]|$)[^|;&]*--visibility'                          && deny "repo-visibility" "$c"

# secret read / exfil
g '\b(cat|less|more|head|tail|xxd|od|strings|bat)\b[^|;&]*(\.env([[:space:]]|$)|\.credentials\.json|\bid_rsa\b|\.pem([[:space:]]|$))' && deny "secret-read" "$c"
g '\b(curl|wget|nc|ncat|scp|rsync|ssh)\b[^|;&]*(\.env([[:space:]]|$)|\.credentials|\bid_rsa\b|secret|token)' && deny "secret-exfil" "$c"

# catastrophic rm against absolute / home roots
g '\brm\b[[:space:]]+(-[A-Za-z]+[[:space:]]+)*(/|~|\$HOME|/root|/etc|/var|/usr|/bin|/lib)([[:space:]/]|$)' && deny "rm-catastrophic" "$c"

exit 0
