#!/usr/bin/env bash
# skill-usage-log.sh — PostToolUse(Read|Skill) skill-usage ledger (#1347).
#
# Appends one owner-only line per skill load so the monthly retroactive audit
# can make keep/cull decisions on evidence instead of memory. Two capture
# paths: the Skill tool, and Read of */skills/*/SKILL.md — the latter is how
# bridge-resolved and file-invoked skills actually load.
#
# Best-effort by contract: every failure path exits 0. A telemetry problem
# must never block a read or fail a session. `report [days]` aggregates the
# ledger for the monthly audit.
set -uo pipefail
trap 'exit 0' EXIT

mode="${1:-log}"

report() { # report [days] — "skill count" lines, newest window first
  days="${2:-30}"
  ledger="${CCC_CLAUDE_DIR:-$HOME/.claude}/state/skill-usage/usage.jsonl"
  [ -f "$ledger" ] || exit 0
  since="$(date -u -d "-${days} days" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || since=""
  jq -r --arg since "$since" 'select(.ts >= $since) | .skill' "$ledger" 2>/dev/null \
    | sort | uniq -c | sort -rn | awk '{ printf "%s %s\n", $2, $1 }'
  exit 0
}
[ "$mode" = "report" ] && report "$@"

{ input="$(cat 2>/dev/null)"; } 2>/dev/null
[ -n "$input" ] || exit 0
[ ${#input} -le 65536 ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)" || exit 0
skill=""
case "$tool" in
  Read)
    fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)" || exit 0
    case "$fp" in
      */skills/*/SKILL.md | */skills/*/SKILL.md\#*)
        skill="$(printf '%s' "$fp" | sed -E 's#.*/skills/([^/]+)/SKILL\.md.*#\1#')"
        ;;
    esac
    ;;
  Skill)
    skill="$(printf '%s' "$input" | jq -r '.tool_input.skill // .tool_input.command // empty' 2>/dev/null)" || exit 0
    skill="${skill#/}"
    skill="${skill%% *}"
    ;;
  *)
    exit 0
    ;;
esac
skill="$(printf '%s' "$skill" | tr -cd 'a-z0-9-')"
[ -n "$skill" ] || exit 0

state="${CCC_CLAUDE_DIR:-$HOME/.claude}/state/skill-usage"
mkdir -p "$state" 2>/dev/null || exit 0
chmod 700 "$state" 2>/dev/null || true
ledger="$state/usage.jsonl"
lock="$state/.usage.lock"
{
  flock -x 9
  printf '{"ts":"%s","skill":"%s","tool":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$skill" "$tool" >>"$ledger" 2>/dev/null || true
  chmod 600 "$ledger" 2>/dev/null || true
} 9>>"$lock" 2>/dev/null || true
exit 0
