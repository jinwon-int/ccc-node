#!/usr/bin/env bash
# Tests for ccc-skill-autosave.sh + install-skill-autosave-cron.sh — hermetic,
# no provider/network calls (claude is stubbed like in skill-review.test.sh).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUTOSAVE="$HERE/ccc-skill-autosave.sh"
INSTALLER="$HERE/install-skill-autosave-cron.sh"
REVIEW="$HERE/../claude/hooks/skill-review.sh"
AUTOINSTALL="$HERE/../claude/hooks/skill-review/autoinstall.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_transcript() {
  local path="$1" turns="${2:-6}"
  mkdir -p "$(dirname "$path")"
  : > "$path"
  for i in $(seq 1 "$turns"); do
    printf '{"type":"user","message":{"content":"please automate recurring release checklist %s"}}\n' "$i" >> "$path"
    printf '{"type":"assistant","message":{"content":[{"type":"text","text":"step %s"},{"type":"tool_use","name":"Bash","input":{"command":"git status --short"}}]}}\n' "$i" >> "$path"
  done
}

# --- fixture: fake claude CLI (drafting model), fake scanner, fake crontab ---
mkdir -p "$TMP/bin"
cat > "$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
cat <<'JSON'
{"skill_candidates":[{"name":"release-checklist","category":"ops","summary":"Capture the recurring release checklist.","reason":"Transcript repeats a release flow.","evidence_excerpt":"automate recurring release checklist","skill_md":"---\nname: release-checklist\ndescription: Capture release checklist procedures.\n---\n\n# Release Checklist\n\n## When to Use\n- Recurring release verification.\n\n## Procedure\n1. Inspect git state.\n\n## Safety\n- No raw secrets.\n\n## Verification\n- Output recorded.\n"}]}
JSON
SH
chmod +x "$TMP/bin/claude"
PATH="$TMP/bin:$PATH"

SCAN="$TMP/scan.sh"
printf '#!/usr/bin/env bash\necho scanned > "$SCAN_TOUCH"\n' > "$SCAN"
chmod +x "$SCAN"

STATE="$TMP/state"
PROJECTS="$TMP/projects"
SPOOL="$TMP/spool"
TRANS="$PROJECTS/-root--work/bridge-sess-1.jsonl"
make_transcript "$TRANS" 6
mkdir -p "$STATE"

run_autosave() {
  CCC_STATE_DIR="$STATE" CLAUDE_PROJECTS_DIR="$PROJECTS" CCC_PUSH_SPOOL="$SPOOL" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" SCAN_TOUCH="$TMP/scan.touched" \
  CLAUDE_SKILLS_DIR="$TMP/skills" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
  CCC_NODE=testnode bash "$AUTOSAVE" run
}

# --- 1) full sweep: scan + draft + ledger + spool notification ---------------
run_autosave; rc=$?
ok "autosave exits 0" '[ "$rc" = 0 ]'
ok "scanner invoked" '[ -f "$TMP/scan.touched" ]'
for _ in $(seq 1 40); do
  find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | grep -q . && break
  sleep 0.25
done
ok "draft staged from bridge transcript" 'find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | grep -q .'
ok "draft not installed as live skill" '[ ! -e "$TMP/skills/release-checklist/SKILL.md" ]'
ok "ledger records session" 'grep -q "^bridge-sess-1	" "$STATE/skill-autosave.seen"'
ok "owner notification queued in spool" 'ls "$SPOOL"/*SkillAutosave*.json >/dev/null 2>&1'
ok "notification counts pending drafts" 'jq -r ".text" "$SPOOL"/*SkillAutosave*.json 2>/dev/null | grep -q "1건"'
ok "notification has dedup key" 'jq -r ".dedup" "$SPOOL"/*SkillAutosave*.json 2>/dev/null | grep -q "SkillAutosave:1"'

# --- 2) rerun without transcript growth: no re-draft, no duplicate notify ----
before_drafts="$(find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | wc -l | tr -d '[:space:]')"
before_spool="$(ls "$SPOOL" 2>/dev/null | wc -l | tr -d '[:space:]')"
run_autosave
sleep 1
after_drafts="$(find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | wc -l | tr -d '[:space:]')"
after_spool="$(ls "$SPOOL" 2>/dev/null | wc -l | tr -d '[:space:]')"
ok "unchanged transcript not re-drafted" '[ "$after_drafts" = "$before_drafts" ]'
ok "no duplicate notification for same pending count" '[ "$after_spool" = "$before_spool" ]'

# --- 3) off-switch skips everything ------------------------------------------
touch "$STATE/skill-autosave.disabled"
rm -f "$TMP/scan.touched"
run_autosave
ok "off-switch skips scan" '[ ! -f "$TMP/scan.touched" ]'
rm -f "$STATE/skill-autosave.disabled"

# --- 4) status mode is read-only ----------------------------------------------
out="$(CCC_STATE_DIR="$STATE" bash "$AUTOSAVE" status 2>&1)"
ok "status reports pending count" 'printf "%s" "$out" | grep -q "pending skill drafts:"'

# --- 5) auto mode (#355): sweep drives machine gate + unattended install -------
STATE2="$TMP/state2"; SKILLS2="$TMP/skills2"; SPOOL2="$TMP/spool2"
PROJECTS2="$TMP/projects2"
make_transcript "$PROJECTS2/-root--work/bridge-sess-2.jsonl" 6
mkdir -p "$STATE2"
CCC_STATE_DIR="$STATE2" CLAUDE_PROJECTS_DIR="$PROJECTS2" CCC_PUSH_SPOOL="$SPOOL2" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" SCAN_TOUCH="$TMP/scan2.touched" \
  CLAUDE_SKILLS_DIR="$TMP/skills2" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
  CCC_SKILL_AUTOINSTALL_CMD="$AUTOINSTALL" CCC_SKILL_AUTOSAVE_MODE=auto \
  CCC_NODE=testnode bash "$AUTOSAVE" run
ok "auto mode installs the drafted skill unattended" '[ -f "$SKILLS2/release-checklist/SKILL.md" ]'
# Both layers may legitimately win the install: the sweep's own autoinstall
# pass (trigger=sweep) or the staging pipeline it spawned (trigger=hook-manual).
ok "auto mode records installed-by=autosave ledger" 'jq -e "select(.event==\"install\") | .installed_by == \"autosave\" and (.trigger == \"sweep\" or .trigger == \"hook-manual\")" "$STATE2/skill-autosave-install.jsonl" >/dev/null'
ok "auto mode queues post-hoc install notice" 'ls "$SPOOL2"/*SkillAutoInstall*.json >/dev/null 2>&1'
ok "auto mode suppresses the approval reminder" '! ls "$SPOOL2"/*SkillAutosave-*.json >/dev/null 2>&1'
ok "installed draft archived out of pending queue" 'ls -d "$STATE2/pending-skills/"*.installed-* >/dev/null 2>&1'
out="$(CCC_STATE_DIR="$STATE2" bash "$AUTOSAVE" status 2>&1)"
ok "status reports mode" 'printf "%s" "$out" | grep -q "^mode: approve"'
out="$(CCC_STATE_DIR="$STATE2" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTOSAVE" status 2>&1)"
ok "status reflects auto mode from env" 'printf "%s" "$out" | grep -q "^mode: auto"'

# --- 6) cron installer: dry-run default, idempotent marker line ---------------
CRONFILE="$TMP/crontab.txt"
: > "$CRONFILE"
cat > "$TMP/bin/fakecrontab" <<SH
#!/usr/bin/env bash
if [ "\${1:-}" = "-l" ]; then cat "$CRONFILE"; exit 0; fi
if [ "\${1:-}" = "-" ]; then cat > "$CRONFILE"; exit 0; fi
exit 2
SH
chmod +x "$TMP/bin/fakecrontab"

out="$(CCC_CRONTAB_CMD="$TMP/bin/fakecrontab" CCC_CLAUDE_DIR="$TMP/claude" bash "$INSTALLER" 2>&1)"; rc=$?
ok "installer dry-run exits 0" '[ "$rc" = 0 ]'
ok "installer dry-run does not write crontab" '! grep -q skill-autosave "$CRONFILE"'
ok "installer dry-run previews entry" 'printf "%s" "$out" | grep -q "ccc-node:skill-autosave"'

CCC_CRONTAB_CMD="$TMP/bin/fakecrontab" CCC_CLAUDE_DIR="$TMP/claude" bash "$INSTALLER" --apply >/dev/null 2>&1
ok "installer --apply writes marker line" 'grep -q "ccc-node:skill-autosave" "$CRONFILE"'
CCC_CRONTAB_CMD="$TMP/bin/fakecrontab" CCC_CLAUDE_DIR="$TMP/claude" bash "$INSTALLER" --apply >/dev/null 2>&1
ok "installer is idempotent (single line)" '[ "$(grep -c "ccc-node:skill-autosave" "$CRONFILE")" = 1 ]'
CCC_CRONTAB_CMD="$TMP/bin/fakecrontab" CCC_CLAUDE_DIR="$TMP/claude" bash "$INSTALLER" --remove --apply >/dev/null 2>&1
ok "installer --remove clears entry" '! grep -q "ccc-node:skill-autosave" "$CRONFILE"'

# --- 7) fleet autonomy guard (#386): kill halts the whole sweep ---------------
# kill must stop everything BEFORE the deterministic scan runs — no scan, no
# drafting LLM call, no pending-draft staging — while dry-run/active proceed so
# drafts still stage for human review (the install layer self-guards).
STATE3="$TMP/state3"; PROJECTS3="$TMP/projects3"; SPOOL3="$TMP/spool3"
make_transcript "$PROJECTS3/-root--work/bridge-sess-3.jsonl" 6
mkdir -p "$STATE3"
run_autosave3() {
  CCC_STATE_DIR="$STATE3" CLAUDE_PROJECTS_DIR="$PROJECTS3" CCC_PUSH_SPOOL="$SPOOL3" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" SCAN_TOUCH="$TMP/scan3.touched" \
  CLAUDE_SKILLS_DIR="$TMP/skills3" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
  CCC_NODE=testnode "$@" bash "$AUTOSAVE" run
}

# 7a) kill via env var
rm -f "$TMP/scan3.touched"
run_autosave3 env CCC_AUTONOMY=kill; rc=$?
ok "autonomy=kill exits 0" '[ "$rc" = 0 ]'
ok "autonomy=kill skips scan" '[ ! -f "$TMP/scan3.touched" ]'
ok "autonomy=kill stages no draft" '! find "$STATE3/pending-skills" -name SKILL.md 2>/dev/null | grep -q .'
ok "autonomy=kill logs reason" 'grep -q "reason=autonomy-kill" "$STATE3/skill-autosave.log"'

# 7b) kill via state file
rm -f "$TMP/scan3.touched"
touch "$STATE3/autonomy.kill"
run_autosave3
ok "autonomy.kill file skips scan" '[ ! -f "$TMP/scan3.touched" ]'
rm -f "$STATE3/autonomy.kill"

# 7c) dry-run does NOT halt the sweep (drafting/human-gate path still runs)
rm -f "$TMP/scan3.touched"
run_autosave3 env CCC_AUTONOMY=dry-run
ok "autonomy=dry-run still runs the sweep (scan invoked)" '[ -f "$TMP/scan3.touched" ]'

# 7d) status surfaces the autonomy state
out="$(CCC_STATE_DIR="$STATE3" CCC_AUTONOMY=kill bash "$AUTOSAVE" status 2>&1)"
ok "status reflects autonomy=kill" 'printf "%s" "$out" | grep -q "^autonomy: kill"'

echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
