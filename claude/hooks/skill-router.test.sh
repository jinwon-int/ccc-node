#!/usr/bin/env bash
# Tests for skill-router.sh / lib/skill_router.py (ccc-node #2645) — the
# UserPromptSubmit routing hook, the routability gate and the measurement
# report. Hermetic: a synthetic skills root under a private CCC_CLAUDE_DIR,
# private state, no network. The hook's fail-open contract (always exit 0,
# silent when unsure) is asserted directly.
# shellcheck disable=SC2034  # out / rc / before are read via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/skill-router.sh"
MOD="$HERE/lib/skill_router.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export HOME="$TMP/home"
export CCC_CLAUDE_DIR="$HOME/.claude"
export CODEX_HOME="$HOME/.codex"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
export CCC_SKILL_LISTING_CORE="$TMP/core.txt"
unset CCC_SKILL_ROUTER
SKILLS="$CCC_CLAUDE_DIR/skills"; mkdir -p "$SKILLS" "$CODEX_HOME/skills" "$CCC_STATE_DIR"
chmod 700 "$CCC_CLAUDE_DIR"

skill() { # <root> <name> <description>
  mkdir -p "$1/$2"
  printf -- '---\nname: %s\ndescription: %s\n---\n\n# %s\n\nbody\n' "$2" "$3" "$2" > "$1/$2/SKILL.md"
}
skill "$SKILLS" bridge-restart-detached "Use when restarting the telegram bridge or gateway service from inside its own session: detach restart and verification into a systemd transient unit first."
skill "$SKILLS" wiki-operating-log "Use when a durable operating log entry, decision or runbook must be recorded in the family wiki through the PR-first write-path flow."
skill "$SKILLS" hwpx-report-build "Use when producing a Korean hwpx report document that must inherit an institutional form template without Hangul Office installed."
skill "$SKILLS" merge-queue-landing "Use when a reviewed pull request must be enqueued into the GitHub merge queue and its landing confirmed after CI checks pass."
skill "$SKILLS" firewall-port-audit "Use when auditing ufw firewall allow rules and listening ports on a node after a CVE advisory or before exposing a service."
skill "$SKILLS" core-pr-flow "Use when committing, pushing, opening or merging a pull request in fleet repositories: exact-head checks, cross-account review, squash merge."
skill "$CODEX_HOME/skills" codex-only-probe "Use when probing codex runtime skill roots for the router index test: unique zebra quokka tokens."
printf '# core set\ncore-pr-flow\n' > "$CCC_SKILL_LISTING_CORE"
# Not a skill: no frontmatter name match / status off / symlink / oversize.
mkdir -p "$SKILLS/mismatch"; printf -- '---\nname: other\ndescription: Use when x.\n---\n' > "$SKILLS/mismatch/SKILL.md"
mkdir -p "$SKILLS/retired"; printf -- '---\nname: retired\ndescription: Use when retired skill tokens appear in a prompt about retired things.\nstatus: deprecated\n---\n' > "$SKILLS/retired/SKILL.md"
ln -s "$SKILLS/wiki-operating-log" "$SKILLS/linked"

# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
run_hook() { # run_hook <prompt> [session] -> $out $rc
  local payload
  payload="$(jq -nc --arg p "$1" --arg s "${2:-}" '{prompt:$p} + (if $s != "" then {session_id:$s} else {} end)')"
  out="$(printf '%s' "$payload" | bash "$HOOK" 2>/dev/null)"; rc=$?
}
ctx() { jq -r '.hookSpecificOutput.additionalContext' <<<"$out"; }
names() { ctx | grep -oE '^[0-9]+\. `[a-z0-9-]+`' | sed -E 's/.*`([a-z0-9-]+)`/\1/'; }

# ---- index ------------------------------------------------------------------
out="$(python3 "$MOD" index --rebuild --json)"
ok "index scans claude + codex roots and skips mismatched/deprecated/symlinked entries" \
  'jq -e ".skills == 7 and .by_runtime.claude == 6 and .by_runtime.codex == 1 and .core_excluded == 1" <<<"$out" >/dev/null'
ok "index cache is owner-only" '[ "$(stat -c %a "$CCC_STATE_DIR/skill-router/index.json")" = 600 ]'
# shellcheck disable=SC2034  # before is read via eval inside ok()
before="$(jq -r .built_at "$CCC_STATE_DIR/skill-router/index.json")"
python3 "$MOD" index --json >/dev/null
ok "unchanged roots reuse the cached index" '[ "$(jq -r .built_at "$CCC_STATE_DIR/skill-router/index.json")" = "$before" ]'
skill "$SKILLS" late-arrival "Use when a skill installed after the index was built must still be routable without a manual rebuild: pelican tokens."
sleep 1; touch "$SKILLS"
out="$(python3 "$MOD" index --json)"
ok "a changed root listing rebuilds the index" 'jq -e ".skills == 8" <<<"$out" >/dev/null'

# ---- hook: suggestions ----------------------------------------------------------
run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-1
ok "a Korean ops prompt routes through the synonym map to the matching skill" \
  '[ "$rc" = 0 ] && [ "$(names | head -1)" = bridge-restart-detached ]'
ok "output is a UserPromptSubmit additionalContext record" \
  'jq -e ".hookSpecificOutput.hookEventName == \"UserPromptSubmit\"" <<<"$out" >/dev/null'
ok "the injected text never echoes the prompt" '! grep -q "돌려서" <<<"$out"'
ok "the injected text stays within budget" '[ "$(ctx | wc -c)" -le 760 ]'
ok "suggestion ledger row is body-free (names + scores + session only)" \
  'jq -e ".session == \"sess-1\" and (.candidates[0].name == \"bridge-restart-detached\") and (keys == [\"candidates\",\"session\",\"ts\"])" "$CCC_STATE_DIR/skill-router/suggestions.jsonl" >/dev/null'
ok "last-<session> file lists the candidate names" \
  'jq -e ".candidates | index(\"bridge-restart-detached\") != null" "$CCC_STATE_DIR/skill-router/last-sess-1.json" >/dev/null'
ok "ledger files are owner-only" '[ "$(stat -c %a "$CCC_STATE_DIR/skill-router/suggestions.jsonl")" = 600 ] && [ "$(stat -c %a "$CCC_STATE_DIR/skill-router/last-sess-1.json")" = 600 ]'

run_hook "hwpx 한글 보고서를 기관 양식 그대로 만들어줘" sess-2
ok "English identifiers inside a Korean prompt route too" '[ "$(names | head -1)" = hwpx-report-build ]'

run_hook "please commit and push, open the pull request and squash merge it after the cross-account review" sess-3
ok "a prompt best served by a core skill yields no suggestion (its description is already listed)" '[ -z "$out" ]'
run_hook "enqueue the reviewed pull request into the merge queue and confirm the landing after the checks pass" sess-4
ok "a non-core skill comparable to the core winner is still suggested" 'grep -q merge-queue-landing <<<"$(names)"'
ok "the core skill itself is never suggested" '! grep -q core-pr-flow <<<"$(names)"'

# ---- hook: silence ------------------------------------------------------------------
for p in "1" "2637 승인" "1. 승인" "ok" "/skillsuggest" "고마워 잘했다" "응 그렇게 해"; do
  run_hook "$p" sess-5
  ok "no suggestion for '$p' (short / approval / slash / no signal)" '[ "$rc" = 0 ] && [ -z "$out" ]'
done
ok "silent prompts leave no last-session file" '[ ! -e "$CCC_STATE_DIR/skill-router/last-sess-5.json" ]'
run_hook "tell me about zebra quokka probing in the codex runtime roots" sess-6
ok "codex-root skills are routable" '[ "$(names | head -1)" = codex-only-probe ]'

CCC_SKILL_ROUTER=0 run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-7
ok "CCC_SKILL_ROUTER=0 silences the hook" '[ "$rc" = 0 ] && [ -z "$out" ]'
: > "$CCC_STATE_DIR/skill-router.off"
run_hook "브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘" sess-8
ok "state/skill-router.off silences the hook" '[ -z "$out" ]'
rm -f "$CCC_STATE_DIR/skill-router.off"

out="$(printf 'not json' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "malformed hook input fails open" '[ "$rc" = 0 ] && [ -z "$out" ]'
out="$(printf '' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "empty input fails open" '[ "$rc" = 0 ] && [ -z "$out" ]'
# Here-string, not a pipe: the hook exits before reading stdin when python3 is
# missing, and a producer process would die of SIGPIPE (rc 141 under pipefail).
payload="$(jq -nc '{prompt:"브리지 재시작 전에 분리 유닛으로 돌려서 검증까지 해줘"}')"
out="$(PATH="$TMP/nopy" /bin/bash "$HOOK" <<<"$payload" 2>/dev/null)"; rc=$?
ok "missing python3 fails open" '[ "$rc" = 0 ] && [ -z "$out" ]'

# ---- check-description (autoinstall gate) ----------------------------------------------
out="$(python3 "$MOD" check-description --name copycat --description "Use when a durable operating log entry, decision or runbook must be recorded in the family wiki through the PR-first write-path flow." --json)"; rc=$?
ok "a description copied from an installed skill is shadowed (rc 1)" \
  '[ "$rc" = 1 ] && jq -e ".ok == false and .reason == \"shadowed-by-installed-skill\" and .best_other.name == \"wiki-operating-log\"" <<<"$out" >/dev/null'
out="$(python3 "$MOD" check-description --name matrix-room-mode-switch --description "Use when a Matrix room must move between alert and agent modes and the sender bots need a coordinated restart afterwards." --json)"; rc=$?
ok "a distinctive description passes (rc 0)" '[ "$rc" = 0 ] && jq -e ".ok == true and .distinct_tokens >= 6" <<<"$out" >/dev/null'
out="$(python3 "$MOD" check-description --name tiny --description "Use when deploying." 2>&1)"; rc=$?
ok "a description with too few content tokens fails" '[ "$rc" = 1 ] && grep -q "too-few-content-tokens" <<<"$out"'
out="$(python3 "$MOD" check-description --name x 2>&1)"; rc=$?
ok "check-description without a description is a usage error (rc 2)" '[ "$rc" = 2 ]'
# shellcheck disable=SC2034  # rc is read via eval inside ok()
out="$(python3 "$MOD" check-description --name wiki-operating-log --description "Use when a durable operating log entry, decision or runbook must be recorded in the family wiki through the PR-first write-path flow." --json)"; rc=$?
ok "re-checking an installed skill against itself is not a self-shadow" '[ "$rc" = 0 ]'

# ---- report -------------------------------------------------------------------------------
USAGE="$CCC_STATE_DIR/skill-usage"; mkdir -p "$USAGE"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"ts":"%s","skill":"bridge-restart-detached","tool":"Skill","runtime":"claude","suggested":true}\n{"ts":"%s","skill":"hwpx-report-build","tool":"Skill","runtime":"claude","suggested":false}\n{"ts":"%s","skill":"core-pr-flow","tool":"Skill","runtime":"claude"}\n{"ts":"%s","skill":"wiki-operating-log","tool":"Read","runtime":"claude","suggested":true}\n{"ts":"2020-01-01T00:00:00Z","skill":"ancient","tool":"Skill","runtime":"claude","suggested":true}\n' "$now" "$now" "$now" "$now" > "$USAGE/usage.jsonl"
out="$(python3 "$MOD" report --days 7 --json)"
ok "report counts prompts with suggestions in the window" 'jq -e ".prompts_with_suggestions >= 3" <<<"$out" >/dev/null'
ok "report hit rate uses tagged Skill invocations only (1 of 2 tagged → 0.5), Read rows and untagged rows excluded" \
  'jq -e ".skill_invocations == 3 and .invocations_tagged == 2 and .invocations_following_a_suggestion == 1 and .hit_rate == 0.5" <<<"$out" >/dev/null'
ok "report lists suggested-never-used and used-never-suggested" \
  'jq -e "(.suggested_never_used | map(.[0]) | index(\"merge-queue-landing\") != null) and (.used_never_suggested | map(.[0]) | index(\"core-pr-flow\") != null)" <<<"$out" >/dev/null'
out="$(python3 "$MOD" report --days 7)"
ok "text report renders the headline lines" 'grep -q "followed a suggestion" <<<"$out" && grep -q "prompts with suggestions" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
