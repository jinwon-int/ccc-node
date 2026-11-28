#!/usr/bin/env bash
# skill-usage-log.sh — PostToolUse(Read|Skill|Bash) skill-usage ledger (#1347).
#
# Appends one owner-only line per skill load so the monthly retroactive audit
# can make keep/cull decisions on evidence instead of memory. Three capture
# paths: the Skill tool, Read of */skills/*/SKILL.md — how bridge-resolved and
# file-invoked skills load — and a Bash reader (cat/sed/head/…) of that path,
# which is how agents load a skill body most often (#2748: 63 of 88 loads on
# one node in 14 days, none of them in the ledger before this capture).
# A Bash row carries the skill name and tool only, never the command text.
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
# (#2881 proposal 1) An audit/review run that reads SKILL.md bodies sets
# CCC_SKILL_USAGE_SUPPRESS=1 so the reads never become use evidence. Exact
# value "1" only; anything else logs as before.
[ "${CCC_SKILL_USAGE_SUPPRESS:-}" = 1 ] && exit 0

{ input="$(cat 2>/dev/null)"; } 2>/dev/null
[ -n "$input" ] || exit 0
[ ${#input} -le 65536 ] || exit 0
# Hot-path exit before any fork: Bash runs on most tool calls, and only a
# payload naming a SKILL.md (Read/Bash) or the Skill tool can produce a row.
case "$input" in
  *SKILL.md* | *'"Skill"'*) ;;
  *) exit 0 ;;
esac
command -v jq >/dev/null 2>&1 || exit 0

# Bash: a reader command's own segment must name the skill path. Quoted
# strings without a SKILL.md are masked first (grep -E "a|b" must not split),
# then segments split on && || | newline and "; " (a bare ";" stays), so
# `git add …/SKILL.md && cat notes` does not count. Readers include grep/rg —
# searching one skill's body is consulting it. sed/perl -i and a redirect into
# a SKILL.md are writes, not loads. The path must start at a word/path
# boundary so `pending-skills/<draft>/SKILL.md` and a `<name>` placeholder
# never match. At most 8 distinct names per call.
JQ_BASH_SKILLS='
  (.tool_input.command // "") | tostring
  | gsub("(?<q>'"'"'[^'"'"']*'"'"'|\"[^\"]*\")"; if (.q | test("SKILL\\.md")) then .q else "Q" end)
  | [splits("&&|\\|\\||\\||\n|;\\s")]
  | map(select(test("^\\s*(?:(?:sudo|do|then|else|xargs|command|time|nice)\\s+)*(?:cat|head|tail|sed|less|more|nl|awk|bat|batcat|grep|egrep|rg)(?:\\s|$)")))
  | map(select((test("^\\s*(?:\\S+\\s+)*?(?:sed|perl)\\s(?:.*\\s)?(?:-[A-Za-z]*i|--in-place)") or test(">\\s*\\S*SKILL\\.md")) | not))
  | map([scan("(?:^|[\\s/\"'"'"'=}:])skills/([a-z0-9][a-z0-9-]{0,63})/SKILL\\.md(?=[\\s\"'"'"')]|$)")[]])
  | flatten | unique | .[:8][]'

tool="$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)" || exit 0
skills=""
case "$tool" in
  Read)
    fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)" || exit 0
    case "$fp" in
      */skills/*/SKILL.md | */skills/*/SKILL.md\#*)
        skills="$(printf '%s' "$fp" | sed -E 's#.*/skills/([^/]+)/SKILL\.md.*#\1#')"
        ;;
    esac
    ;;
  Skill)
    skills="$(printf '%s' "$input" | jq -r '.tool_input.skill // .tool_input.command // empty' 2>/dev/null)" || exit 0
    skills="${skills#/}"
    skills="${skills%% *}"
    ;;
  Bash)
    skills="$(printf '%s' "$input" | jq -r "$JQ_BASH_SKILLS" 2>/dev/null)" || exit 0
    ;;
  *)
    exit 0
    ;;
esac
[ -n "$skills" ] || exit 0

# Which runtime loaded the skill (#1739). This hook is wired into Claude Code,
# so the default is claude; other callers (the piri extension) say who they are.
runtime="$(printf '%s' "${CCC_SKILL_USAGE_RUNTIME:-claude}" | tr -cd 'a-z0-9-' | cut -c1-32)"
[ -n "$runtime" ] || runtime="claude"

state="${CCC_CLAUDE_DIR:-$HOME/.claude}/state/skill-usage"
mkdir -p "$state" 2>/dev/null || exit 0
chmod 700 "$state" 2>/dev/null || true
ledger="$state/usage.jsonl"
lock="$state/.usage.lock"
# Routing measurement (#2645): when the skill router suggested candidates for
# this session's latest prompt, say whether this use followed one of them.
# Absent suggestion file → no field (the pre-#2645 row shape), so the report
# can tell "no suggestion" from "suggestion ignored".
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null | tr -cd 'A-Za-z0-9_.-' | cut -c1-128)"
last=""
if [ -n "$sid" ]; then
  last="${CCC_STATE_DIR:-${CCC_CLAUDE_DIR:-$HOME/.claude}/state}/skill-router/last-$sid.json"
  { [ -f "$last" ] && [ ! -L "$last" ]; } || last=""
fi
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rows=""
while IFS= read -r skill; do
  skill="$(printf '%s' "$skill" | tr -cd 'a-z0-9-')"
  [ -n "$skill" ] || continue
  suggested=""
  if [ -n "$last" ]; then
    if jq -e --arg s "$skill" '.candidates | index($s) != null' "$last" >/dev/null 2>&1; then
      suggested=',"suggested":true'
    else
      suggested=',"suggested":false'
    fi
  fi
  rows="$rows$(printf '{"ts":"%s","skill":"%s","tool":"%s","runtime":"%s"%s}' \
    "$ts" "$skill" "$tool" "$runtime" "$suggested")
"
done <<EOF_SKILLS
$skills
EOF_SKILLS
[ -n "$rows" ] || exit 0
{
  flock -x 9
  printf '%s' "$rows" >>"$ledger" 2>/dev/null || true
  chmod 600 "$ledger" 2>/dev/null || true
} 9>>"$lock" 2>/dev/null || true
exit 0
