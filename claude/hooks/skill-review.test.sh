#!/usr/bin/env bash
# Tests for skill-review.sh / skill-review/extract.sh — no provider/network calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REVIEW="$HERE/skill-review.sh"
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
cat > "$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
cat <<'JSON'
{"skill_candidates":[{"name":"deploy-checklist","category":"ops","summary":"Capture a recurring deploy checklist.","reason":"The transcript repeats a multi-step deploy verification flow.","evidence_excerpt":"automate recurring deploy checklist","skill_md":"---\nname: deploy-checklist\ndescription: Capture deploy checklist procedures.\n---\n\n# Deploy Checklist\n\n## When to Use\n- Use when deploy verification repeats.\n\n## Procedure\n1. Inspect git state.\n2. Run the verified checklist.\n\n## Safety\n- Never store raw secrets.\n\n## Verification\n- Confirm the checklist output is recorded.\n"}]}
JSON
SH
chmod +x "$TMP/bin/claude"
PATH="$TMP/bin:$PATH"

out="$(payload sess-1 "$TRANS" "/root/work" | CCC_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 bash "$REVIEW" sessionend 2>&1)"; rc=$?
ok "skill-review hook exits 0" '[ "$rc" = 0 ]'
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
