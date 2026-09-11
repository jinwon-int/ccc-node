#!/usr/bin/env bash
# harness: umask-rerun
# Tests for skill-review.sh / skill-review/extract.sh — no provider/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REVIEW="$HERE/skill-review.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

# Sandbox every fallback path. These scripts resolve their state dir from
# CCC_SKILL_REVIEW_STATE_DIR/CCC_CLAUDE_DIR/HOME; if a fixture forgets one, the
# fallback must land in TMP and never in the real node queue. A run of this
# suite once archived live drafts out of ~/.claude/state/pending-skills because
# an unset anchor fell through to the operator's home.
export HOME="$TMP/home"
export CCC_CLAUDE_DIR="$TMP/home/.claude"
mkdir -p "$CCC_CLAUDE_DIR/state" "$CCC_CLAUDE_DIR/skills"
chmod 700 "$CCC_CLAUDE_DIR/state" "$CCC_CLAUDE_DIR/skills"

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

argv_is_deny_all() {
  local file="$1"
  awk '
    $0 == "<--tools>" {
      getline
      if ($0 == "<>") tools=1
    }
    $0 == "<--disallowedTools>" {
      getline
      if ($0 == "<mcp__*>") mcp=1
    }
    $0 == "<--strict-mcp-config>" { strict=1 }
    $0 == "<--permission-mode>" {
      getline
      if ($0 == "<dontAsk>") mode=1
    }
    END { exit !(tools && mcp && strict && mode) }
  ' "$file"
}

make_transcript() {
  local path="$1" turns="${2:-6}"
  mkdir -p "$(dirname "$path")"
  : > "$path"
  for i in $(seq 1 "$turns"); do
    printf '{"type":"user","message":{"content":"please automate recurring deploy checklist %s"}}\n' "$i" >> "$path"
    printf '{"type":"assistant","message":{"content":[{"type":"text","text":"step %s"},{"type":"tool_use","name":"Bash","input":{"command":"git status --short"}}]}}\n' "$i" >> "$path"
  done
}

payload() { jq -nc --arg sid "$1" --arg tp "$2" --arg cwd "$3" '{session_id:$sid, transcript_path:$tp, cwd:$cwd}'; }

STATE="$TMP/state"
SKILLS="$TMP/skills"
TRANS="$TMP/projects/-root--work/sess-1.jsonl"
make_transcript "$TRANS" 5
mkdir -p "$STATE" "$SKILLS"

mkdir -p "$TMP/bin"
write_exec_stub "$TMP/bin/claude" <<'SH'
cat >/dev/null
if [ -n "${CLAUDE_ENV_SNAPSHOT:-}" ]; then
  printf '%s|%s|%s\n' \
    "${CLAUDE_SKILL_REVIEW_BG-unset}" \
    "${CLAUDE_SKILL_REVIEW_INFLIGHT-unset}" \
    "${CLAUDE_DISTILL_INFLIGHT-unset}" \
    > "$CLAUDE_ENV_SNAPSHOT"
fi
if [ -n "${CLAUDE_ARGS_SNAPSHOT:-}" ]; then
  printf '<%s>\n' "$@" > "$CLAUDE_ARGS_SNAPSHOT"
fi
if [ -n "${CLAUDE_TOOL_ENV_SNAPSHOT:-}" ]; then
  printf '<%s>\n' "${CCC_ALLOWED_TOOLS-unset}" > "$CLAUDE_TOOL_ENV_SNAPSHOT"
fi
cat <<'JSON'
{"skill_candidates":[{"name":"deploy-checklist","category":"ops","summary":"Capture a recurring deploy checklist.","reason":"The transcript repeats a multi-step deploy verification flow.","evidence_excerpt":"automate recurring deploy checklist","skill_md":"---\nname: deploy-checklist\ndescription: Capture deploy checklist procedures.\n---\n\n# Deploy Checklist\n\n## When to Use\n- Use when deploy verification repeats.\n\n## Procedure\n1. Inspect git state.\n2. Run the verified checklist.\n\n## Safety\n- Never store raw secrets.\n\n## Verification\n- Confirm the checklist output is recorded.\n"}]}
JSON
SH
chmod +x "$TMP/bin/claude"
write_exec_stub "$TMP/bin/setsid" <<'SH'
exec "$@"
SH
PATH="$TMP/bin:$PATH"

payload sess-1 "$TRANS" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 bash "$REVIEW" sessionend >/dev/null 2>&1; rc=$?
ok "skill-review hook exits 0" '[ "$rc" = 0 ]'
ok "skill-review uses the shared setsid spawn mode" 'grep -q "spawned bg pid=.* mode=setsid" "$STATE/skill-review.log"'
for _ in $(seq 1 30); do
  find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | grep -q . && [ -f "$STATE/approval-needed.log" ] && grep -q "PENDING_SKILL_REVIEW" "$STATE/approval-needed.log" && break
  sleep 0.1
done
ok "skill-review stages SKILL.md" 'find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | grep -q .'
ok "skill-review writes meta" 'find "$STATE/pending-skills" -name meta.json 2>/dev/null | grep -q .'
ok "skill-review does not install live skill" '[ ! -e "$SKILLS/deploy-checklist/SKILL.md" ]'
ok "approval marker written" '[ -f "$STATE/approval-needed.log" ] && grep -q "PENDING_SKILL_REVIEW" "$STATE/approval-needed.log"'
for _ in $(seq 1 30); do
  grep -q "done staged=1" "$STATE/skill-review.log" 2>/dev/null && break
  sleep 0.1
done
ok "last JSON stashed" 'jq -e ".skill_candidates | length == 1" "$STATE/skill-review-last.json" >/dev/null'

# Auto mode (#355): the SessionEnd pipeline hands staged drafts to the machine
# gate, which installs them unattended and archives the draft. Fresh state so
# the approve-mode run above cannot interfere.
STATE_AUTO="$TMP/state-auto"
SKILLS_AUTO="$TMP/skills-auto"
SPOOL_AUTO="$TMP/spool-auto"
mkdir -p "$STATE_AUTO" "$SKILLS_AUTO"
chmod 700 "$STATE_AUTO"
chmod 700 "$SKILLS_AUTO"  # contract-compliant root under any umask (#770)
payload sess-auto "$TRANS" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE_AUTO" CLAUDE_SKILLS_DIR="$SKILLS_AUTO" \
  CCC_PUSH_SPOOL="$SPOOL_AUTO" CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 \
  bash "$REVIEW" sessionend >/dev/null 2>&1; rc=$?
ok "auto-mode hook exits 0" '[ "$rc" = 0 ]'
for _ in $(seq 1 40); do
  [ -f "$SKILLS_AUTO/deploy-checklist/SKILL.md" ] \
    && [ -f "$SKILLS_AUTO/deploy-checklist/.autosave-meta.json" ] \
    && ls -d "$STATE_AUTO/pending-skills/"*.installed-* >/dev/null 2>&1 \
    && ls "$SPOOL_AUTO"/*SkillAutoInstall*.json >/dev/null 2>&1 \
    && break
  sleep 0.25
done
ok "auto mode installs staged draft unattended" '[ -f "$SKILLS_AUTO/deploy-checklist/SKILL.md" ]'
ok "auto mode leaves autosave ledger + marker" 'jq -e ".installed_by == \"autosave\"" "$SKILLS_AUTO/deploy-checklist/.autosave-meta.json" >/dev/null && jq -e "select(.event==\"install\") | .name == \"deploy-checklist\"" "$STATE_AUTO/skill-autosave-install.jsonl" >/dev/null'
ok "auto mode archives the draft" 'ls -d "$STATE_AUTO/pending-skills/"*.installed-* >/dev/null 2>&1'
ok "auto mode queues post-hoc notice" 'ls "$SPOOL_AUTO"/*SkillAutoInstall*.json >/dev/null 2>&1'
ok "auto mode writes no approval marker when nothing stays pending" '! grep -q "PENDING_SKILL_REVIEW" "$STATE_AUTO/approval-needed.log" 2>/dev/null'

# Cooldown should skip a second hook-triggered run when enabled.
: > "$STATE/skill-review.log"
payload sess-1 "$TRANS" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_SKILL_REVIEW_COOLDOWN_SECONDS=9999 bash "$REVIEW" sessionend >/dev/null 2>&1; rc=$?
ok "cooldown run exits 0" '[ "$rc" = 0 ]'
ok "cooldown skip logged" 'grep -q "skip reason=cooldown" "$STATE/skill-review.log"'

# Recursion guard short-circuits before touching state.
: > "$STATE/skill-review.log"
CLAUDE_SKILL_REVIEW_INFLIGHT=1 CCC_SKILL_REVIEW_STATE_DIR="$STATE" bash "$REVIEW" sessionend <<<"$(payload sess-guard "$TRANS" "/root/work")" >/dev/null 2>&1; rc=$?
ok "recursion guard exits 0" '[ "$rc" = 0 ]'
ok "recursion guard logs nothing" '[ ! -s "$STATE/skill-review.log" ]'

# The emergency off-switch must beat a stale/inherited detached-runner marker.
STATE_DISABLED="$TMP/state-disabled"
SNAPSHOT_DISABLED="$TMP/disabled-provider-env"
mkdir -p "$STATE_DISABLED"
: > "$STATE_DISABLED/skill-review.disabled"
CLAUDE_SKILL_REVIEW_BG=1 CLAUDE_SKILL_REVIEW_INFLIGHT=1 \
  CLAUDE_SKILL_REVIEW_TRANSCRIPT="$TRANS" CLAUDE_SKILL_REVIEW_SESSION=sess-disabled \
  CLAUDE_ENV_SNAPSHOT="$SNAPSHOT_DISABLED" CCC_SKILL_REVIEW_STATE_DIR="$STATE_DISABLED" \
  bash "$REVIEW" sessionend >/dev/null 2>&1; rc=$?
ok "disabled background re-entry exits 0" '[ "$rc" = 0 ]'
ok "disabled background re-entry never calls provider" '[ ! -e "$SNAPSHOT_DISABLED" ]'
ok "disabled background re-entry is logged" 'grep -q "skip reason=disabled" "$STATE_DISABLED/skill-review.log"'

# The provider child keeps recursion guards but must not inherit the runner
# marker, otherwise its own SessionEnd hook launches another detached review.
SNAPSHOT_PROVIDER="$TMP/provider-env"
SNAPSHOT_ARGS="$TMP/provider-args"
SNAPSHOT_TOOL_ENV="$TMP/provider-tool-env"
CLAUDE_SKILL_REVIEW_TRANSCRIPT="$TRANS" CLAUDE_SKILL_REVIEW_SESSION=sess-provider \
  CLAUDE_SKILL_REVIEW_BG=1 CLAUDE_SKILL_REVIEW_INFLIGHT=1 CLAUDE_DISTILL_INFLIGHT=1 \
  CLAUDE_ENV_SNAPSHOT="$SNAPSHOT_PROVIDER" CLAUDE_ARGS_SNAPSHOT="$SNAPSHOT_ARGS" \
  CLAUDE_TOOL_ENV_SNAPSHOT="$SNAPSHOT_TOOL_ENV" \
  CCC_ALLOWED_TOOLS="Bash,Edit,Write" \
  bash "$HERE/skill-review/extract.sh" >/dev/null 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "provider environment probe exits 0" '[ "$rc" = 0 ]'
ok "provider drops runner marker and keeps recursion guards" \
  '[ "$(cat "$SNAPSHOT_PROVIDER" 2>/dev/null)" = "unset|1|1" ]'
ok "skill extractor denies built-in and MCP tools despite hostile inherited allowlist" \
  'argv_is_deny_all "$SNAPSHOT_ARGS" && [ "$(cat "$SNAPSHOT_TOOL_ENV")" = "<unset>" ] && ! grep -q "<Bash>\\|<Edit>\\|<Write>" "$SNAPSHOT_ARGS"'

# --- #1654: provider-neutral drafting ---------------------------------------
# A CCC_SKILL_REVIEW_LLM_CMD node drafts without any claude CLI on PATH, and
# the prompt it receives is runtime-neutral with the provider-interpolated
# category. The fake LLM tees its stdin so the prompt itself is assertable.
LLM_SNAPSHOT="$TMP/llm-stdin.txt"
LLM_ARGS_SNAPSHOT="$TMP/llm-args.txt"
mkdir -p "$TMP/bin-llm"
write_exec_stub "$TMP/bin-llm/fake-llm" <<SH
cat > "$LLM_SNAPSHOT"
printf '%s\\n' "\$@" > "$LLM_ARGS_SNAPSHOT"
cat <<'JSON'
{"skill_candidates":[{"name":"neutral-probe","category":"piri","summary":"Probe emitted by the neutral LLM command.","reason":"Synthetic fixture response.","evidence_excerpt":"fixture","skill_md":"---\nname: neutral-probe\ndescription: Probe skill emitted by the fake neutral LLM command fixture.\n---\n\n# Neutral Probe\n\n## When to Use\n- Never; this is a fixture.\n\n## Procedure\n1. Emit.\n\n## Safety\n- No secrets.\n\n## Verification\n- Fixture only.\n"}]}
JSON
SH
chmod +x "$TMP/bin-llm/fake-llm"
rm -f "$LLM_SNAPSHOT" "$LLM_ARGS_SNAPSHOT"
STATE_LLM="$TMP/state-llm"
mkdir -p "$STATE_LLM"; chmod 700 "$STATE_LLM"
CLAUDE_SKILL_REVIEW_TRANSCRIPT="$TRANS" CLAUDE_SKILL_REVIEW_SESSION=sess-llm \
  CLAUDE_SKILL_REVIEW_BG=1 CLAUDE_SKILL_REVIEW_INFLIGHT=1 \
  CCC_SKILL_REVIEW_STATE_DIR="$STATE_LLM" CCC_SKILL_PROVIDER=piri \
  CCC_SKILL_REVIEW_LLM_CMD="$TMP/bin-llm/fake-llm --flag value" \
  PATH="${PATH#"$TMP/bin:"}" \
  bash "$HERE/skill-review/extract.sh" >"$TMP/llm-out.json" 2>/dev/null
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "LLM_CMD drafts without claude on PATH" '[ "$rc" = 0 ] && jq -e ".skill_candidates[0].name == \"neutral-probe\"" >/dev/null <<<"$(cat "$TMP/llm-out.json")"'
ok "LLM_CMD argv is shlex-split (flags reach the command)" \
  'grep -qx -- "--flag" "$LLM_ARGS_SNAPSHOT" && grep -qx "value" "$LLM_ARGS_SNAPSHOT"'
ok "prompt is provider-routed (category=piri)" 'grep -q '\''"category": "piri"'\'' "$LLM_SNAPSHOT"'
ok "prompt drops the Claude-node framing" '! grep -q "Claude Code node" "$LLM_SNAPSHOT"'
ok "prompt forbids runtime couplings in drafts" 'grep -q "never write" "$LLM_SNAPSHOT" && grep -q "agent CLI" "$LLM_SNAPSHOT"'

# meta.json records the staging provider so autoinstall (#1655) and promotion
# can route per-draft without guessing from the process environment.
STATE_PIRI="$TMP/state-piri"
mkdir -p "$STATE_PIRI"; chmod 700 "$STATE_PIRI"
payload sess-piri "$TRANS" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE_PIRI" CLAUDE_SKILLS_DIR="$SKILLS" \
  CCC_SKILL_PROVIDER=piri CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 bash "$REVIEW" sessionend >/dev/null 2>&1
for _ in $(seq 1 30); do
  find "$STATE_PIRI/pending-skills" -name meta.json 2>/dev/null | grep -q . && break
  sleep 0.1
done
# shellcheck disable=SC2034  # read via eval inside ok()
piri_meta="$(find "$STATE_PIRI/pending-skills" -name meta.json 2>/dev/null | head -1)"
ok "piri branch meta records provider=piri" \
  '[ -n "$piri_meta" ] && jq -e ".provider == \"piri\"" >/dev/null "$piri_meta"'
# Unset provider auto-detects claude on this fixture (HOME has ~/.claude, no codex home).
# shellcheck disable=SC2034  # read via eval inside ok()
claude_meta="$(find "$STATE/pending-skills" -name meta.json 2>/dev/null | head -1)"
ok "default branch meta records provider=claude" \
  '[ -n "$claude_meta" ] && jq -e ".provider == \"claude\"" >/dev/null "$claude_meta"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
