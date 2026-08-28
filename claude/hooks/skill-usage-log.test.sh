#!/usr/bin/env bash
# Tests for claude/hooks/skill-usage-log.sh — the PostToolUse(Read|Skill)
# usage ledger (#1347). Hermetic: every case runs against a private
# CCC_CLAUDE_DIR, ambient harness variables must not reach the ledger (#1023),
# and the hook's fail-open contract (always exit 0) is asserted directly.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/skill-usage-log.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

HOME_DIR="$TMP/home"
export CCC_CLAUDE_DIR="$HOME_DIR/.claude"
export HOME="$HOME_DIR"
mkdir -p "$CCC_CLAUDE_DIR"
chmod 700 "$CCC_CLAUDE_DIR"
LEDGER="$CCC_CLAUDE_DIR/state/skill-usage/usage.jsonl"

run_hook() { # run_hook <stdin-json>
  printf '%s' "$1" | bash "$HOOK"
}

rc=0
run_hook '{"tool_name":"Read","tool_input":{"file_path":"/home/x/.claude/skills/gh-pr-flow/SKILL.md"}}' || rc=$?
ok "read of a SKILL.md appends one ledger line and exits 0" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$LEDGER")" = 1 ]'
ok "ledger line carries the skill name and the Read tool" \
  'jq -e "select(.skill == \"gh-pr-flow\" and .tool == \"Read\" and .ts != null)" "$LEDGER" >/dev/null'
ok "ledger is owner-only and so is its directory" \
  '[ "$(stat -c %a "$LEDGER")" = 600 ] && [ "$(stat -c %a "$CCC_CLAUDE_DIR/state/skill-usage")" = 700 ]'

before="$(wc -l < "$LEDGER")"
run_hook '{"tool_name":"Read","tool_input":{"file_path":"/home/x/notes/todo.md"}}'
ok "reads of non-skill paths do not touch the ledger" \
  '[ "$(wc -l < "$LEDGER")" = "$before" ]'

rc=0
run_hook '{"tool_name":"Skill","tool_input":{"skill":"/wiki-record extra-args"}}' || rc=$?
ok "Skill invocations are captured with the leading slash and args stripped" \
  '[ "$rc" = 0 ] && jq -e "select(.skill == \"wiki-record\" and .tool == \"Skill\")" "$LEDGER" >/dev/null'

run_hook '{"tool_name":"Skill","tool_input":{"command":"skillsuggest"}}'
ok "Skill command-style invocations resolve to the skill name" \
  'jq -e "select(.skill == \"skillsuggest\")" "$LEDGER" >/dev/null'

before="$(wc -l < "$LEDGER")"
rc=0
printf 'not json at all\x00\x01' | bash "$HOOK" || rc=$?
ok "garbage stdin fails open with exit 0 and no ledger write" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$LEDGER")" = "$before" ]'

rc=0
python3 -c "print(70000 * 'x', end='')" | bash "$HOOK" || rc=$?
ok "oversized stdin fails open with exit 0" \
  '[ "$rc" = 0 ] && [ "$(wc -l < "$LEDGER")" = "$before" ]'

# Monthly report (#1347): counts per skill inside the window only.
printf '{"ts":"2020-01-01T00:00:00Z","skill":"stale-one","tool":"Read"}\n' >> "$LEDGER"
printf '{"ts":"%s","skill":"wiki-record","tool":"Read"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LEDGER"
report="$(bash "$HOOK" report 30)"
ok "report aggregates windowed counts newest-first" \
  'grep -qx "wiki-record 2" <<<"$report" && ! grep -q "stale-one" <<<"$report" && grep -qx "gh-pr-flow 1" <<<"$report"'

old_home="$TMP/empty-home"
mkdir -p "$old_home"
out="$(HOME="$old_home" CCC_CLAUDE_DIR="$old_home/.claude" bash "$HOOK" report 30)"
ok "report on a missing ledger is silent and fails open" \
  '[ "$out" = "" ]'

rc=0
CCC_CLAUDE_DIR="$TMP/never-created-$$" bash "$HOOK" '{"tool_name":"Read","tool_input":{"file_path":"/a/.claude/skills/x/SKILL.md"}}' </dev/null || rc=$?
ok "unwritable state root still fails open" '[ "$rc" = 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
