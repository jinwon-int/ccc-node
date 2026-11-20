#!/usr/bin/env bash
# Tests for claude/hooks/skill-usage-log.sh — the PostToolUse(Read|Skill|Bash)
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
ok "ledger line attributes the load to the claude runtime by default (#1739)" \
  'jq -e "select(.skill == \"gh-pr-flow\" and .runtime == \"claude\")" "$LEDGER" >/dev/null'
printf '%s' '{"tool_name":"Read","tool_input":{"file_path":"/skills/rt-probe/SKILL.md"}}' \
  | CCC_SKILL_USAGE_RUNTIME=piri bash "$HOOK"
printf '%s' '{"tool_name":"Read","tool_input":{"file_path":"/skills/rt-inject/SKILL.md"}}' \
  | CCC_SKILL_USAGE_RUNTIME='Piri","evil":"1' bash "$HOOK"
ok "an explicit runtime is recorded and sanitized into valid JSON" \
  'jq -e "select(.skill == \"rt-probe\" and .runtime == \"piri\")" "$LEDGER" >/dev/null && jq -e "select(.skill == \"rt-inject\" and .runtime == \"irievil1\" and (has(\"evil\") | not))" "$LEDGER" >/dev/null'
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

# shellcheck disable=SC2034  # before is read via eval inside ok()
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
# shellcheck disable=SC2034  # report is read via eval inside ok()
report="$(bash "$HOOK" report 30)"
ok "report aggregates windowed counts newest-first" \
  'grep -qx "wiki-record 2" <<<"$report" && ! grep -q "stale-one" <<<"$report" && grep -qx "gh-pr-flow 1" <<<"$report"'

old_home="$TMP/empty-home"
mkdir -p "$old_home"
# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(HOME="$old_home" CCC_CLAUDE_DIR="$old_home/.claude" bash "$HOOK" report 30)"
ok "report on a missing ledger is silent and fails open" \
  '[ "$out" = "" ]'

rc=0
# shellcheck disable=SC2034  # rc is read via eval inside ok()
CCC_CLAUDE_DIR="$TMP/never-created-$$" bash "$HOOK" '{"tool_name":"Read","tool_input":{"file_path":"/a/.claude/skills/x/SKILL.md"}}' </dev/null || rc=$?
ok "unwritable state root still fails open" '[ "$rc" = 0 ]'


# ---- #2645: routing measurement tag --------------------------------------------
# With a router suggestion recorded for the session, a use says whether it
# followed one; without a suggestion file the row keeps the pre-#2645 shape.
ROUTER_STATE="$CCC_CLAUDE_DIR/state/skill-router"; mkdir -p "$ROUTER_STATE"
printf '{"ts":"2026-01-01T00:00:00Z","candidates":["gh-pr-flow","wiki-record"]}\n' > "$ROUTER_STATE/last-sess-r1.json"
: > "$LEDGER"
run_hook '{"session_id":"sess-r1","tool_name":"Skill","tool_input":{"skill":"gh-pr-flow"}}'
ok "#2645: a use that follows a suggestion is tagged suggested:true" \
  'jq -e "select(.skill == \"gh-pr-flow\" and .suggested == true)" "$LEDGER" >/dev/null'
run_hook '{"session_id":"sess-r1","tool_name":"Skill","tool_input":{"skill":"self-update"}}'
ok "#2645: a use outside the suggestions is tagged suggested:false" \
  'jq -e "select(.skill == \"self-update\" and .suggested == false)" "$LEDGER" >/dev/null'
run_hook '{"session_id":"sess-none","tool_name":"Skill","tool_input":{"skill":"gh-pr-flow"}}'
ok "#2645: without a suggestion file the row has no suggested field" \
  'jq -e "select(.skill == \"gh-pr-flow\" and (has(\"suggested\") | not))" "$LEDGER" >/dev/null'
run_hook '{"session_id":"../../etc","tool_name":"Skill","tool_input":{"skill":"gh-pr-flow"}}'
ok "#2645: a hostile session id is sanitised, the hook still exits 0 and logs the use" \
  '[ "$(jq -r "select(.skill == \"gh-pr-flow\") | .skill" "$LEDGER" | wc -l)" = 3 ]'

# ---- #2748: Bash readers of a SKILL.md -----------------------------------------
# Agents load skill bodies with cat/sed/head far more often than with Read.
# Only the skill name is recorded; non-reader commands, writes, drafts and
# placeholders never count.
bash_hook() { # bash_hook <command> [session]
  jq -nc --arg c "$1" --arg s "${2:-}" '{tool_name:"Bash",tool_input:{command:$c}} + (if $s == "" then {} else {session_id:$s} end)' | bash "$HOOK"
}
bash_rows() { jq -r 'select(.tool == "Bash") | .skill' "$LEDGER" | sort | tr '\n' ' '; }
: > "$LEDGER"
rc=0
bash_hook 'cat ~/.claude/skills/gh-ci-wait/SKILL.md | head -50' || rc=$?
ok "#2748: cat of a SKILL.md appends one Bash row and exits 0" \
  '[ "$rc" = 0 ] && [ "$(bash_rows)" = "gh-ci-wait " ]'
ok "#2748: a Bash row carries ts/skill/tool/runtime only — never the command text" \
  'jq -e "select(.tool == \"Bash\") | (keys == [\"runtime\",\"skill\",\"tool\",\"ts\"])" "$LEDGER" >/dev/null && ! grep -q "head -50" "$LEDGER"'
: > "$LEDGER"
bash_hook "sed -n '1,80p' /a/.claude/skills/x-one/SKILL.md; head \"\${CCC_CLAUDE_DIR:-\$HOME/.claude}/skills/x-two/SKILL.md\" && cat /a/.claude/skills/x-one/SKILL.md"
ok "#2748: several readers in one call give one row per distinct skill" \
  '[ "$(bash_rows)" = "x-one x-two " ] && [ "$(wc -l < "$LEDGER")" = 2 ]'
: > "$LEDGER"
bash_hook 'git add claude/skills/gh-pr-flow/SKILL.md && git commit -m x'
bash_hook 'ls ~/.claude/skills/wiki-record/SKILL.md; diff -q /a/skills/wiki-record/SKILL.md /b/x'
bash_hook "sed -i 's/a/b/' /a/.claude/skills/self-update/SKILL.md"
bash_hook 'cat > /a/.claude/skills/self-update/SKILL.md <<EOF'
bash_hook 'cat "$STATE/pending-skills/20261118-abc-staged-rollout/SKILL.md"'
bash_hook 'cat /a/.claude/skills/<name>/SKILL.md'
bash_hook 'cat /a/.claude/skills/web-routing/SKILL.md.bak'
bash_hook 'echo hello && cat /etc/hostname'
ok "#2748: non-readers, in-place edits, redirects, drafts, placeholders and backups never count" \
  '[ ! -s "$LEDGER" ]'
: > "$LEDGER"
bash_hook 'cd /x && for f in a; do cat /a/.claude/skills/distill/SKILL.md 2>/dev/null; done'
ok "#2748: a reader after do/&& with a stderr redirect still counts" \
  '[ "$(bash_rows)" = "distill " ]'
: > "$LEDGER"
bash_hook "sed -n '20,33p;55,90p' /a/.claude/skills/gh-ci-wait/SKILL.md"
bash_hook 'grep -n -i -E "merge.queue|enqueue" ~/.claude/skills/gh-pr-flow/SKILL.md | head'
bash_hook "grep -nE 'a|b; c' \"\$HOME/.claude/skills/web-routing/SKILL.md\""
ok "#2748: quoted | and ; never split a segment, a quoted skill path still counts, grep -i counts" \
  '[ "$(bash_rows)" = "gh-ci-wait gh-pr-flow web-routing " ]'
: > "$LEDGER"
bash_hook 'cat /a/.claude/skills/gh-pr-flow/SKILL.md /a/.claude/skills/mcp-add/SKILL.md' sess-r1
ok "#2748: Bash rows get the routing tag like Skill rows" \
  'jq -e "select(.skill == \"gh-pr-flow\" and .tool == \"Bash\" and .suggested == true)" "$LEDGER" >/dev/null && jq -e "select(.skill == \"mcp-add\" and .suggested == false)" "$LEDGER" >/dev/null'
rc=0
# shellcheck disable=SC2034  # rc is read via eval inside ok()
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"cat /a/skills/x/SKILL.md"}' | bash "$HOOK" || rc=$?
ok "#2748: malformed Bash payload fails open with exit 0" '[ "$rc" = 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
