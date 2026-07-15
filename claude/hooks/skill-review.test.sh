#!/usr/bin/env bash
# Tests for skill-review.sh / skill-review/extract.sh — no provider/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REVIEW="$HERE/skill-review.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

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
cat <<'JSON'
{"skill_candidates":[{"name":"deploy-checklist","category":"ops","summary":"Capture a recurring deploy checklist.","reason":"The transcript repeats a multi-step deploy verification flow.","evidence_excerpt":"automate recurring deploy checklist","skill_md":"---\nname: deploy-checklist\ndescription: Capture deploy checklist procedures.\n---\n\n# Deploy Checklist\n\n## When to Use\n- Use when deploy verification repeats.\n\n## Procedure\n1. Inspect git state.\n2. Run the verified checklist.\n\n## Safety\n- Never store raw secrets.\n\n## Verification\n- Confirm the checklist output is recorded.\n"}]}
JSON
SH
chmod +x "$TMP/bin/claude"
write_exec_stub "$TMP/bin/setsid" <<'SH'
exec "$@"
SH
PATH="$TMP/bin:$PATH"

out="$(payload sess-1 "$TRANS" "/root/work" | CCC_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 bash "$REVIEW" sessionend 2>&1)"; rc=$?
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
out="$(payload sess-auto "$TRANS" "/root/work" | CCC_STATE_DIR="$STATE_AUTO" CLAUDE_SKILLS_DIR="$SKILLS_AUTO" \
  CCC_PUSH_SPOOL="$SPOOL_AUTO" CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 \
  bash "$REVIEW" sessionend 2>&1)"; rc=$?
ok "auto-mode hook exits 0" '[ "$rc" = 0 ]'
for _ in $(seq 1 40); do
  [ -f "$SKILLS_AUTO/deploy-checklist/SKILL.md" ] && break
  sleep 0.25
done
ok "auto mode installs staged draft unattended" '[ -f "$SKILLS_AUTO/deploy-checklist/SKILL.md" ]'
ok "auto mode leaves autosave ledger + marker" 'jq -e ".installed_by == \"autosave\"" "$SKILLS_AUTO/deploy-checklist/.autosave-meta.json" >/dev/null && jq -e "select(.event==\"install\") | .name == \"deploy-checklist\"" "$STATE_AUTO/skill-autosave-install.jsonl" >/dev/null'
ok "auto mode archives the draft" 'ls -d "$STATE_AUTO/pending-skills/"*.installed-* >/dev/null 2>&1'
ok "auto mode queues post-hoc notice" 'ls "$SPOOL_AUTO"/*SkillAutoInstall*.json >/dev/null 2>&1'
ok "auto mode writes no approval marker when nothing stays pending" '! grep -q "PENDING_SKILL_REVIEW" "$STATE_AUTO/approval-needed.log" 2>/dev/null'

# Cooldown should skip a second hook-triggered run when enabled.
: > "$STATE/skill-review.log"
out="$(payload sess-1 "$TRANS" "/root/work" | CCC_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_SKILL_REVIEW_COOLDOWN_SECONDS=9999 bash "$REVIEW" sessionend 2>&1)"; rc=$?
ok "cooldown run exits 0" '[ "$rc" = 0 ]'
ok "cooldown skip logged" 'grep -q "skip reason=cooldown" "$STATE/skill-review.log"'

# Recursion guard short-circuits before touching state.
: > "$STATE/skill-review.log"
out="$(CLAUDE_SKILL_REVIEW_INFLIGHT=1 CCC_STATE_DIR="$STATE" bash "$REVIEW" sessionend <<<"$(payload sess-guard "$TRANS" "/root/work")" 2>&1)"; rc=$?
ok "recursion guard exits 0" '[ "$rc" = 0 ]'
ok "recursion guard logs nothing" '[ ! -s "$STATE/skill-review.log" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
