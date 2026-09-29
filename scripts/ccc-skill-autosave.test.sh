#!/usr/bin/env bash
# Tests for ccc-skill-autosave.sh + install-skill-autosave-cron.sh — hermetic,
# no provider/network calls (claude is stubbed like in skill-review.test.sh).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUTOSAVE="$HERE/ccc-skill-autosave.sh"
INSTALLER="$HERE/install-skill-autosave-cron.sh"
REVIEW="$HERE/../claude/hooks/skill-review.sh"
AUTOINSTALL="$HERE/../claude/hooks/skill-review/autoinstall.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
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
{"skill_candidates":[{"name":"release-checklist","category":"ops","summary":"Capture the recurring release checklist.","reason":"Transcript repeats a release flow.","evidence_excerpt":"automate recurring release checklist","skill_md":"---\nname: release-checklist\ndescription: Use when capturing release checklist procedures.\n---\n\n# Release Checklist\n\n## When to Use\n- Recurring release verification.\n\n## Procedure\n1. Inspect git state.\n\n## Safety\n- No raw secrets.\n\n## Verification\n- Output recorded.\n"}]}
JSON
SH
chmod +x "$TMP/bin/claude"
PATH="$TMP/bin:$PATH"

SCAN="$TMP/scan.sh"
printf '#!/usr/bin/env bash\necho scanned > "$SCAN_TOUCH"\n' > "$SCAN"
chmod +x "$SCAN"

PROMOTER="$TMP/promoter.py"
cat > "$PROMOTER" <<'PY'
#!/usr/bin/env python3
import os
from pathlib import Path
import sys
args = " ".join(sys.argv[1:])
with Path(os.environ["PROMOTION_TOUCH"]).open("a", encoding="utf-8") as handle:
    handle.write(args + "\n")
# #1766: record whether the publisher edge secret reached this child — the
# presence flag only, never the value, so a leak in the fixture cannot become a
# leak in the suite output.
secret_touch = os.environ.get("PROMOTION_SECRET_TOUCH")
if secret_touch:
    state = "present" if os.environ.get("A2A_EDGE_SECRET") else "absent"
    with Path(secret_touch).open("a", encoding="utf-8") as handle:
        handle.write(args + " secret=" + state + "\n")
print('{"ok":true}')
PY
chmod +x "$PROMOTER"

STATE="$TMP/state"
PROJECTS="$TMP/projects"
SPOOL="$TMP/spool"
TRANS="$PROJECTS/-root--work/bridge-sess-1.jsonl"
make_transcript "$TRANS" 6
mkdir -p "$STATE"

run_autosave() {
  CCC_STATE_DIR="$STATE" CLAUDE_PROJECTS_DIR="$PROJECTS" CCC_PUSH_SPOOL="$SPOOL" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" SCAN_TOUCH="$TMP/scan.touched" \
  CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion.touched" \
  CLAUDE_SKILLS_DIR="$TMP/skills" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
  CCC_NODE=testnode bash "$AUTOSAVE" run
}

# --- 1) full sweep: scan + draft + ledger + spool notification ---------------
run_autosave; rc=$?
ok "autosave exits 0" '[ "$rc" = 0 ]'
ok "scanner invoked" '[ -f "$TMP/scan.touched" ]'
ok "active sweep invokes central promoter in live mode" 'grep -qx "run" "$TMP/promotion.touched"'
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
# shellcheck disable=SC2034  # before_drafts is read via eval inside ok()
before_drafts="$(find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | wc -l | tr -d '[:space:]')"
# shellcheck disable=SC2034  # before_spool is read via eval inside ok()
before_spool="$(ls "$SPOOL" 2>/dev/null | wc -l | tr -d '[:space:]')"
run_autosave
sleep 1
# shellcheck disable=SC2034  # after_drafts is read via eval inside ok()
after_drafts="$(find "$STATE/pending-skills" -name SKILL.md 2>/dev/null | wc -l | tr -d '[:space:]')"
# shellcheck disable=SC2034  # after_spool is read via eval inside ok()
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
STATE2="$TMP/state2"; SPOOL2="$TMP/spool2"
# shellcheck disable=SC2034  # SKILLS2 is read via eval inside ok()
SKILLS2="$TMP/skills2"
PROJECTS2="$TMP/projects2"
make_transcript "$PROJECTS2/-root--work/bridge-sess-2.jsonl" 6
mkdir -p "$STATE2"
chmod 700 "$STATE2"
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
  CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion3.touched" \
  CLAUDE_SKILLS_DIR="$TMP/skills3" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
  CCC_NODE=testnode "$@" bash "$AUTOSAVE" run
}

# 7a) kill via env var
rm -f "$TMP/scan3.touched"
rm -f "$TMP/promotion3.touched"
run_autosave3 env CCC_AUTONOMY=kill
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "autonomy=kill exits 0" '[ "$rc" = 0 ]'
ok "autonomy=kill skips scan" '[ ! -f "$TMP/scan3.touched" ]'
ok "autonomy=kill skips central promoter" '[ ! -f "$TMP/promotion3.touched" ]'
ok "autonomy=kill stages no draft" '! find "$STATE3/pending-skills" -name SKILL.md 2>/dev/null | grep -q .'
ok "autonomy=kill logs reason" 'grep -q "reason=autonomy-kill" "$STATE3/skill-autosave.log"'
ok "autonomy=kill records to shared fleet ledger" 'grep -q "\"layer\":\"skill-autosave\"" "$STATE3/autonomy-ledger.jsonl" && grep -q "\"state\":\"kill\"" "$STATE3/autonomy-ledger.jsonl"'

# 7b) kill via state file
rm -f "$TMP/scan3.touched"
touch "$STATE3/autonomy.kill"
run_autosave3
ok "autonomy.kill file skips scan" '[ ! -f "$TMP/scan3.touched" ]'
rm -f "$STATE3/autonomy.kill"

# 7c) dry-run does NOT halt the sweep (drafting/human-gate path still runs)
rm -f "$TMP/scan3.touched"
rm -f "$TMP/promotion3.touched"
run_autosave3 env CCC_AUTONOMY=dry-run
ok "autonomy=dry-run still runs the sweep (scan invoked)" '[ -f "$TMP/scan3.touched" ]'
ok "autonomy=dry-run previews central promotion" 'grep -qx "run --dry-run" "$TMP/promotion3.touched"'

# 7d) status surfaces the autonomy state
# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(CCC_STATE_DIR="$STATE3" CCC_AUTONOMY=kill bash "$AUTOSAVE" status 2>&1)"
ok "status reflects autonomy=kill" 'printf "%s" "$out" | grep -q "^autonomy: kill"'

# --- 8) codex drafting branch (#1353, opt-in, default off) -------------------
# Rollouts are projected by the REAL codex-rollout-normalize.py (no stub — the
# mapping is the contract), then dispatched through the SAME real skill-review.sh
# with CCC_SKILL_PROVIDER=codex and the normalized tree as CLAUDE_PROJECTS_DIR.
CODEX_HOME4="$TMP/codex"
CODEX_SESS="$CODEX_HOME4/sessions/2026/08/31"
mkdir -p "$CODEX_SESS"
cat > "$CODEX_SESS/rollout-2026-08-31T09-00-00-aaaa-bbbb.jsonl" <<'EOF'
{"timestamp":"2026-08-31T00:00:00Z","type":"session_meta","payload":{"session_id":"codexsess-1","cwd":"/root/app","originator":"codex_cli"}}
{"timestamp":"2026-08-31T00:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"deployment health check please"}]}}
{"timestamp":"2026-08-31T00:00:02Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"checking service status"}]}}
{"timestamp":"2026-08-31T00:00:03Z","type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"{\"command\":[\"bash\",\"-lc\",\"systemctl status app --no-pager\"]}"}}
{"timestamp":"2026-08-31T00:00:04Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"service is active"}]}}
EOF
cat > "$CODEX_SESS/rollout-2026-08-31T09-05-00-cccc-dddd.jsonl" <<'EOF'
{"timestamp":"2026-08-31T00:05:00Z","type":"session_meta","payload":{"session_id":"machinesess-1","cwd":"/root/app","originator":"codex_exec"}}
{"timestamp":"2026-08-31T00:05:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"machine driven run"}]}}
EOF

STATE4="$TMP/state4"; PROJECTS4="$TMP/projects4"; SPOOL4="$TMP/spool4"
mkdir -p "$STATE4" "$PROJECTS4"
chmod 700 "$STATE4"

# 8a) default OFF: the sessions tree is not even walked.
run4() {
  env CCC_STATE_DIR="$STATE4" CLAUDE_PROJECTS_DIR="$PROJECTS4" CCC_PUSH_SPOOL="$SPOOL4" \
    CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
    CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion4.touched" \
    CCC_SKILL_CODEX_NORMALIZE_CMD="$HERE/codex-rollout-normalize.py" \
    CODEX_HOME="$CODEX_HOME4" CLAUDE_SKILLS_DIR="$TMP/skills4" \
    CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode \
    bash "$AUTOSAVE" run
}
run4
ok "codex branch default off logs not-enabled" 'grep -q "codex skipped reason=not-enabled" "$STATE4/skill-autosave.log"'
ok "default off walks no sessions (no normalized tree)" '[ ! -d "$STATE4/codex-normalized" ]'

# 8b) opt-in via state file: projection + dispatch + shared-state ledger.
printf '1\n' > "$STATE4/skill-autosave.codex-drafting"
run4
# shellcheck disable=SC2034  # proj4 is read via eval inside ok()
proj4="$STATE4/codex-normalized/-root-app/codexsess-1.jsonl"
ok "opt-in projects the rollout into the branch-local tree" '[ -f "$proj4" ]'
ok "projection lands in the Claude shape (Bash tool_use)" \
  'grep -q "\"type\": \"tool_use\", \"name\": \"Bash\"" "$proj4"'
excluded_ledgered=0
while IFS=$'\t' read -r key _rest; do
  # shellcheck disable=SC2034  # excluded_ledgered is read via eval inside ok()
  [ "$key" = "rollout-2026-08-31T09-05-00-cccc-dddd" ] && excluded_ledgered=1
done < "$STATE4/skill-autosave.codex-seen"
ok "codex_exec session excluded and ledgered" \
  'grep -q "codex excluded session=rollout-2026-08-31T09-05-00-cccc-dddd reason=codex_exec" "$STATE4/skill-autosave.log" && [ "$excluded_ledgered" = 1 ]'
ok "excluded session produced no normalized file" '[ ! -e "$STATE4/codex-normalized/-root-app/machinesess-1.jsonl" ]'
ok "codex dispatch through real skill-review logged ok" 'grep -q "codex review ok session=rollout-2026-08-31T09-00-00-aaaa-bbbb" "$STATE4/skill-autosave.log"'
drafted_ledgered=0
while IFS=$'\t' read -r key _rest; do
  # shellcheck disable=SC2034  # drafted_ledgered is read via eval inside ok()
  [ "$key" = "rollout-2026-08-31T09-00-00-aaaa-bbbb" ] && drafted_ledgered=1
done < "$STATE4/skill-autosave.codex-seen"
ok "codex sweep summary counted one draft" \
  '[ "$drafted_ledgered" = 1 ]'

# 8c) rerun without growth: regrowth ledger prevents re-normalization/re-draft.
# shellcheck disable=SC2034  # log_before is read via eval inside ok()
log_before="$(grep -c "codex review ok" "$STATE4/skill-autosave.log")"
run4
# shellcheck disable=SC2034  # log_after is read via eval inside ok()
log_after="$(grep -c "codex review ok" "$STATE4/skill-autosave.log")"
ok "unchanged rollout not re-drafted" '[ "$log_after" = "$log_before" ]'

# 8d) opt-in on a node without codex sessions is a clean no-op.
STATE5="$TMP/state5"; mkdir -p "$STATE5"; chmod 700 "$STATE5"
printf '1\n' > "$STATE5/skill-autosave.codex-drafting"
env CCC_STATE_DIR="$STATE5" CLAUDE_PROJECTS_DIR="$TMP/projects5" CCC_PUSH_SPOOL="$TMP/spool5" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion5.touched" \
  CCC_SKILL_CODEX_NORMALIZE_CMD="$HERE/codex-rollout-normalize.py" \
  CODEX_HOME="$TMP/no-codex-home" CLAUDE_SKILLS_DIR="$TMP/skills5" \
  CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode bash "$AUTOSAVE" run
ok "missing sessions tree is a clean skip" 'grep -q "codex skipped reason=no-sessions-tree" "$STATE5/skill-autosave.log"'

# 8d) opt-in on a node without codex sessions is a clean no-op.
STATE5="$TMP/state5"; mkdir -p "$STATE5"; chmod 700 "$STATE5"
printf '1\n' > "$STATE5/skill-autosave.codex-drafting"
env CCC_STATE_DIR="$STATE5" CLAUDE_PROJECTS_DIR="$TMP/projects5" CCC_PUSH_SPOOL="$TMP/spool5" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion5.touched" \
  CCC_SKILL_CODEX_NORMALIZE_CMD="$HERE/codex-rollout-normalize.py" \
  CODEX_HOME="$TMP/no-codex-home" CLAUDE_SKILLS_DIR="$TMP/skills5" \
  CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode bash "$AUTOSAVE" run
ok "missing sessions tree is a clean skip" 'grep -q "codex skipped reason=no-sessions-tree" "$STATE5/skill-autosave.log"'

# --- 9) piri drafting branch (opt-in, default off; mirrors the codex branch) --
# Sessions are projected by the REAL piri-session-normalize.py (no stub — the
# mapping is the contract), then dispatched through the SAME real skill-review.sh
# with CCC_SKILL_PROVIDER=piri and the normalized tree as CLAUDE_PROJECTS_DIR.
PIRI_HOME4="$TMP/piri-agent"
PIRI_SESS="$PIRI_HOME4/sessions/-home-gongmyoung--"
mkdir -p "$PIRI_SESS"
cat > "$PIRI_SESS/2026-09-05T09-00-00-1111-2222.jsonl" <<'EOF'
{"type":"session","version":3,"id":"pirisess-1","timestamp":"2026-09-05T09:00:00.000Z","cwd":"/home/gongmyoung"}
{"type":"message","id":"u1","timestamp":"2026-09-05T09:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"백업 상태 확인해줘"}]}}
{"type":"message","id":"a1","timestamp":"2026-09-05T09:00:02.000Z","message":{"role":"assistant","content":[{"type":"thinking","text":"noise"}]}}
{"type":"message","id":"a2","timestamp":"2026-09-05T09:00:03.000Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"c1","name":"bash","arguments":{"command":"ls -la /var/backups | head"}}]}}
{"type":"message","id":"a3","timestamp":"2026-09-05T09:00:04.000Z","message":{"role":"assistant","content":[{"type":"text","text":"백업 정상입니다"}]}}
{"type":"message","id":"a4","timestamp":"2026-09-05T09:00:05.000Z","message":{"role":"assistant","content":[{"type":"text","text":"검증 결과를 기록했습니다"}]}}
EOF
# (a4 lifts the projection to MIN_TURNS=4: since #1932 a shorter session is
# a no-budget "not-reviewable" skip, not a "review ok" dispatch.)

run9() {
  env CCC_STATE_DIR="$STATE9" CLAUDE_PROJECTS_DIR="$PROJECTS9" CCC_PUSH_SPOOL="$SPOOL9" \
    CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
    CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion9.touched" \
    CCC_SKILL_PIRI_NORMALIZE_CMD="$HERE/piri-session-normalize.py" \
    PIRI_CODING_AGENT_DIR="$PIRI_HOME4" CLAUDE_SKILLS_DIR="$TMP/skills9" \
    CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode \
    bash "$AUTOSAVE" run
}
STATE9="$TMP/state9"; PROJECTS9="$TMP/projects9"; SPOOL9="$TMP/spool9"
mkdir -p "$STATE9" "$PROJECTS9"
chmod 700 "$STATE9"

# 9a) default OFF: the sessions tree is not even walked.
run9
ok "piri branch default off logs not-enabled" 'grep -q "piri skipped reason=not-enabled" "$STATE9/skill-autosave.log"'
ok "default off walks no piri sessions (no normalized tree)" '[ ! -d "$STATE9/piri-normalized" ]'

# 9b) opt-in via state file: projection + dispatch + shared-state ledger.
printf '1\n' > "$STATE9/skill-autosave.piri-drafting"
run9
# shellcheck disable=SC2034  # proj9 is read via eval inside ok()
proj9="$STATE9/piri-normalized/-home-gongmyoung/pirisess-1.jsonl"
ok "opt-in projects the piri session into the branch-local tree" '[ -f "$proj9" ]'
ok "projection lands in the Claude shape (Bash tool_use)" \
  'grep -q "\"type\": \"tool_use\", \"name\": \"Bash\"" "$proj9" && grep -q "ls -la /var/backups" "$proj9"'
ok "thinking noise is not projected" '! grep -q "noise" "$proj9"'
ok "piri dispatch through real skill-review logged ok" 'grep -q "piri review ok session=2026-09-05T09-00-00-1111-2222" "$STATE9/skill-autosave.log"'
piri_ledgered=0
while IFS=$'\t' read -r key _rest; do
  # shellcheck disable=SC2034  # piri_ledgered is read via eval inside ok()
  [ "$key" = "2026-09-05T09-00-00-1111-2222" ] && piri_ledgered=1
done < "$STATE9/skill-autosave.piri-seen"
ok "piri sweep summary counted one draft" '[ "$piri_ledgered" = 1 ]'
# #1652: the sweep summary must count piri drafts so the settle window (and the
# notification step) sees a piri-only run as draft-bearing.
ok "sweep summary logs piri_drafted" 'grep -q "sweep done drafted_sessions=0 codex_drafted=0 piri_drafted=1" "$STATE9/skill-autosave.log"'

# 9c) rerun without growth: regrowth ledger prevents re-normalization/re-draft.
# shellcheck disable=SC2034  # log_before9 is read via eval inside ok()
log_before9="$(grep -c "piri review ok" "$STATE9/skill-autosave.log")"
run9
# shellcheck disable=SC2034  # log_after9 is read via eval inside ok()
log_after9="$(grep -c "piri review ok" "$STATE9/skill-autosave.log")"
ok "unchanged piri session not re-drafted" '[ "$log_after9" = "$log_before9" ]'

# --- 10) danso drafting branch (opt-in, #1660; mirrors the piri branch) ------
# Journal tree: <CCC_DANSO_STATE_DIR>/{journals,chatgpt-journals,glm-journals}
# [-audience/<scope>]/<uuid>.jsonl. A Pi-v3-compatible fixture (plus the danso
# custom operation row) is projected by the REAL piri-session-normalize.py and
# dispatched through skill-review.sh with CCC_SKILL_PROVIDER=danso.
DANSO_STATE_FIX="$TMP/danso-state-fixture"
JROOT="$DANSO_STATE_FIX/journals-audience/private-b3362e2106be28b2f3221f38d9624b84"
mkdir -p "$JROOT" && chmod 700 "$JROOT"
cat > "$JROOT/1e0a9d2e-0000-4000-8000-000000000001.jsonl" <<'EOF'
{"type":"session","version":3,"id":"dansosess-1","timestamp":"2026-09-11T08:00:00.000Z","cwd":"/home/gongmyoung"}
{"type":"custom","customType":"danso.operation.v1","payload":{"op":"checkpoint"}}
{"type":"message","id":"u1","timestamp":"2026-09-11T08:00:01.000Z","message":{"role":"user","content":[{"type":"text","text":"매일 백업 검증 절차를 밟아줘"}]}}
{"type":"message","id":"a1","timestamp":"2026-09-11T08:00:02.000Z","message":{"role":"assistant","content":[{"type":"thinking","text":"noise"}]}}
{"type":"message","id":"a2","timestamp":"2026-09-11T08:00:03.000Z","message":{"role":"assistant","content":[{"type":"toolCall","id":"c1","name":"bash","arguments":{"command":"ls -la /var/backups | head"}}]}}
{"type":"message","id":"a3","timestamp":"2026-09-11T08:00:04.000Z","message":{"role":"assistant","content":[{"type":"text","text":"백업 정상입니다"}]}}
{"type":"message","id":"a4","timestamp":"2026-09-11T08:00:05.000Z","message":{"role":"assistant","content":[{"type":"text","text":"검증 결과를 기록했습니다"}]}}
EOF
run10() {
  env CCC_STATE_DIR="$STATE_DANSO" CLAUDE_PROJECTS_DIR="$PROJECTS_DANSO" CCC_PUSH_SPOOL="$SPOOL_DANSO" \
    CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
    CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion10.touched" \
    CCC_NODE=testnode CCC_DANSO_STATE_DIR="$DANSO_STATE_FIX" \
    bash "$AUTOSAVE" run
}
STATE_DANSO="$TMP/state-danso10"; PROJECTS_DANSO="$TMP/projects10d"; SPOOL_DANSO="$TMP/spool10d"
mkdir -p "$STATE_DANSO" "$PROJECTS_DANSO"
chmod 700 "$STATE_DANSO"

# 10a) default OFF.
run10
ok "danso branch default off logs not-enabled" 'grep -q "danso skipped reason=not-enabled" "$STATE_DANSO/skill-autosave.log"'
ok "default off walks no danso journals (no normalized tree)" '[ ! -d "$STATE_DANSO/danso-normalized" ]'

# 10b) opt-in via state file: projection + dispatch with provider=danso.
printf '1\n' > "$STATE_DANSO/skill-autosave.danso-drafting"
run10
ok "opt-in projects the danso journal into the branch-local tree" \
  '[ -f "$STATE_DANSO/danso-normalized/journals-audience-private-b3362e2106be28b2f3221f38d9624b84/dansosess-1.jsonl" ]'
ok "danso custom operation rows are not projected" \
  '! grep -q "danso.operation" "$STATE_DANSO/danso-normalized/journals-audience-private-b3362e2106be28b2f3221f38d9624b84/dansosess-1.jsonl"'
ok "danso dispatch through real skill-review logged ok" \
  'grep -q "danso review ok session=1e0a9d2e-0000-4000-8000-000000000001" "$STATE_DANSO/skill-autosave.log"'
ok "sweep summary logs danso_drafted" \
  'grep -q "sweep done drafted_sessions=0 codex_drafted=0 piri_drafted=0 danso_drafted=1" "$STATE_DANSO/skill-autosave.log"'
# shellcheck disable=SC2034  # read via eval inside ok()
danso_meta="$(find "$STATE_DANSO/pending-skills" -name meta.json 2>/dev/null | head -1)"
ok "danso pending draft meta records provider=danso" \
  '[ -n "$danso_meta" ] && jq -e ".provider == \"danso\"" >/dev/null "$danso_meta"'

# 10c) no CCC_DANSO_STATE_DIR is a clean skip.
rm -rf "$STATE_DANSO/danso-normalized" "$STATE_DANSO/skill-autosave.danso-seen"
: > "$STATE_DANSO/skill-autosave.log"
env CCC_STATE_DIR="$STATE_DANSO" CLAUDE_PROJECTS_DIR="$PROJECTS_DANSO" CCC_PUSH_SPOOL="$SPOOL_DANSO" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  CCC_SKILL_PROMOTION_CMD="$PROMOTER" CCC_NODE=testnode \
  CCC_SKILL_DANSO_DRAFTING=1 bash "$AUTOSAVE" run
ok "opt-in without CCC_DANSO_STATE_DIR logs no-state-dir" \
  'grep -q "danso skipped reason=no-state-dir" "$STATE_DANSO/skill-autosave.log"'

# 10d) regrowth ledger prevents re-drafting an unchanged journal.
run10
# shellcheck disable=SC2034  # read via eval inside ok()
before10="$(grep -c "danso review ok" "$STATE_DANSO/skill-autosave.log")"
run10
# shellcheck disable=SC2034  # read via eval inside ok()
after10="$(grep -c "danso review ok" "$STATE_DANSO/skill-autosave.log")"
ok "unchanged danso journal not re-drafted" '[ "$after10" = "$before10" ]'

# 9d) opt-in on a node without piri sessions is a clean no-op.
STATE10="$TMP/state10"; mkdir -p "$STATE10"; chmod 700 "$STATE10"
printf '1\n' > "$STATE10/skill-autosave.piri-drafting"
env CCC_STATE_DIR="$STATE10" CLAUDE_PROJECTS_DIR="$TMP/projects10" CCC_PUSH_SPOOL="$TMP/spool10" \
  CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion10.touched" \
  CCC_SKILL_PIRI_NORMALIZE_CMD="$HERE/piri-session-normalize.py" \
  PIRI_CODING_AGENT_DIR="$TMP/no-piri-home" CLAUDE_SKILLS_DIR="$TMP/skills10" \
  CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode bash "$AUTOSAVE" run
ok "missing piri sessions tree is a clean skip" 'grep -q "piri skipped reason=no-sessions-tree" "$STATE10/skill-autosave.log"'

# --- 11) #1766: block 2d loads the publisher edge env before `collect` --------
# The intake review round is dispatched by `collect` and needs A2A_EDGE_SECRET.
# The autosave cron never sourced the edge env file, so every intake PR opened
# and then stalled on a2a/receipts FAILURE with a silent dispatch_secret_missing
# (fleet-skills #183 #205 #212 #217 #221 #225). These cases pin the three halves
# of the fix: the secret reaches the collect child, it reaches nothing else, and
# a node without an edge env file still sweeps successfully.
STATE11="$TMP/state11"; mkdir -p "$STATE11"; chmod 700 "$STATE11"
EDGE_ENV_FILE="$TMP/edge.env"
# `export`-style, like the real publisher file; the value is a fixture-only
# sentinel so the leak assertions below can grep for it.
printf 'export A2A_EDGE_SECRET=edge-secret-fixture-1766\n' > "$EDGE_ENV_FILE"
chmod 600 "$EDGE_ENV_FILE"

run11() { # $1 = CCC_A2A_EDGE_ENV value (may point at a missing file)
  env CCC_STATE_DIR="$STATE11" CLAUDE_PROJECTS_DIR="$TMP/projects11" \
    CCC_PUSH_SPOOL="$TMP/spool11" CCC_SKILL_REVIEW_CMD="$REVIEW" \
    CCC_SKILL_SCAN_CMD="$SCAN" SCAN_TOUCH="$TMP/scan11.touched" \
    CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion11.touched" \
    PROMOTION_SECRET_TOUCH="$TMP/promotion11.secret" CCC_A2A_EDGE_ENV="$1" \
    CLAUDE_SKILLS_DIR="$TMP/skills11" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
    CCC_NODE=testnode bash "$AUTOSAVE" run
}

# 11a) edge env file present: the collect child sees the secret.
run11 "$EDGE_ENV_FILE"; rc=$?
ok "#1766: sweep with an edge env file still exits 0" '[ "$rc" = 0 ]'
ok "#1766: collect child sees A2A_EDGE_SECRET" \
  'grep -qx "collect secret=present" "$TMP/promotion11.secret"'
ok "#1766: staging child is left alone (only collect dispatches)" \
  'grep -qx "run secret=absent" "$TMP/promotion11.secret"'
ok "#1766: edge-env state is logged without the secret value" \
  'grep -q "promotion-collect edge-env=loaded" "$STATE11/skill-autosave.log"'
ok "#1766: the secret value never reaches the autosave log" \
  '! grep -q "edge-secret-fixture-1766" "$STATE11/skill-autosave.log"'
ok "#1766: the secret value never reaches the notification spool" \
  '! grep -rq "edge-secret-fixture-1766" "$TMP/spool11" 2>/dev/null'
# shellcheck disable=SC2034  # read via eval inside ok()
status11="$(CCC_STATE_DIR="$STATE11" CCC_A2A_EDGE_ENV="$EDGE_ENV_FILE" bash "$AUTOSAVE" status 2>&1)"
ok "#1766: status reports the edge env as present, never its value" \
  'printf "%s" "$status11" | grep -q "^a2a edge env: present" && ! printf "%s" "$status11" | grep -q "edge-secret-fixture-1766"'
# shellcheck disable=SC2034  # read via eval inside ok()
status11b="$(CCC_STATE_DIR="$STATE11" CCC_A2A_EDGE_ENV="$TMP/no-such-edge.env" bash "$AUTOSAVE" status 2>&1)"
ok "#1766: status reports a missing edge env as absent" \
  'printf "%s" "$status11b" | grep -q "^a2a edge env: absent"'

# 11b) autonomy dry-run keeps the preview lane and still loads the env.
: > "$TMP/promotion11.secret"
env CCC_AUTONOMY=dry-run CCC_STATE_DIR="$STATE11" CLAUDE_PROJECTS_DIR="$TMP/projects11" \
  CCC_PUSH_SPOOL="$TMP/spool11" CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  SCAN_TOUCH="$TMP/scan11.touched" CCC_SKILL_PROMOTION_CMD="$PROMOTER" \
  PROMOTION_TOUCH="$TMP/promotion11.touched" PROMOTION_SECRET_TOUCH="$TMP/promotion11.secret" \
  CCC_A2A_EDGE_ENV="$EDGE_ENV_FILE" CLAUDE_SKILLS_DIR="$TMP/skills11" \
  CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode bash "$AUTOSAVE" run
ok "#1766: dry-run collect keeps --dry-run and still loads the edge env" \
  'grep -qx "collect --dry-run secret=present" "$TMP/promotion11.secret"'

# 11c) absent edge env file: non-fatal, collect still runs, nothing pretends.
STATE11B="$TMP/state11b"; mkdir -p "$STATE11B"; chmod 700 "$STATE11B"
: > "$TMP/promotion11.secret"
env CCC_STATE_DIR="$STATE11B" CLAUDE_PROJECTS_DIR="$TMP/projects11" \
  CCC_PUSH_SPOOL="$TMP/spool11" CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  SCAN_TOUCH="$TMP/scan11.touched" CCC_SKILL_PROMOTION_CMD="$PROMOTER" \
  PROMOTION_TOUCH="$TMP/promotion11.touched" PROMOTION_SECRET_TOUCH="$TMP/promotion11.secret" \
  CCC_A2A_EDGE_ENV="$TMP/no-such-edge.env" CLAUDE_SKILLS_DIR="$TMP/skills11" \
  CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode bash "$AUTOSAVE" run
rc=$?
ok "#1766: a missing edge env file is non-fatal" '[ "$rc" = 0 ]'
ok "#1766: collect still runs without an edge env file" \
  'grep -qx "collect secret=absent" "$TMP/promotion11.secret"'
ok "#1766: the absent edge env is logged as such" \
  'grep -q "promotion-collect edge-env=absent" "$STATE11B/skill-autosave.log"'

# 11d) an unreadable/broken edge env file must not break the sweep or leak.
BROKEN_ENV="$TMP/broken-edge.env"
printf 'echo leaked-edge-secret-1766\nthis is ( not ) valid shell\n' > "$BROKEN_ENV"
STATE11C="$TMP/state11c"; mkdir -p "$STATE11C"; chmod 700 "$STATE11C"
: > "$TMP/promotion11.secret"
env CCC_STATE_DIR="$STATE11C" CLAUDE_PROJECTS_DIR="$TMP/projects11" \
  CCC_PUSH_SPOOL="$TMP/spool11" CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
  SCAN_TOUCH="$TMP/scan11.touched" CCC_SKILL_PROMOTION_CMD="$PROMOTER" \
  PROMOTION_TOUCH="$TMP/promotion11.touched" PROMOTION_SECRET_TOUCH="$TMP/promotion11.secret" \
  CCC_A2A_EDGE_ENV="$BROKEN_ENV" CLAUDE_SKILLS_DIR="$TMP/skills11" \
  CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode bash "$AUTOSAVE" run
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "#1766: a malformed edge env file is non-fatal" '[ "$rc" = 0 ]'
ok "#1766: a malformed edge env file still runs collect" \
  'grep -qx "collect secret=absent" "$TMP/promotion11.secret"'
ok "#1766: edge env output never reaches the summary or the log" \
  '! grep -rq "leaked-edge-secret-1766" "$STATE11C" 2>/dev/null'

# --- 12) #2011: review is the default, auto nodes migrate only explicitly ------
# The owner moved the fleet default to review (draft -> human review ->
# install). An unset mode already resolves to the human gate; an existing
# `auto` node is never flipped silently — it gets an advisory and migrates via
# the explicit, idempotent `set-mode` command.
ok "#2011: auto sweep logs the review advisory" \
  'grep -q "mode-advisory mode=auto source=env recommended=review" "$STATE2/skill-autosave.log"'
STATE12="$TMP/state12"; mkdir -p "$STATE12"; chmod 700 "$STATE12"
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" status 2>&1)"
ok "#2011: unset mode resolves to the human gate (default source)" \
  'printf "%s" "$out" | grep -q "^mode: approve (source: default"'
ok "#2011: default mode prints no auto advisory" '! printf "%s" "$out" | grep -q "^advisory:"'
printf 'review\n' > "$STATE12/skill-autosave.mode"
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" status 2>&1)"
ok "#2011: 'review' in the state file is the human gate" \
  'printf "%s" "$out" | grep -q "^mode: approve (source: state-file"'
printf 'auto\n' > "$STATE12/skill-autosave.mode"
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" status 2>&1)"
ok "#2011: an auto node keeps auto until migrated (no silent flip)" \
  'printf "%s" "$out" | grep -q "^mode: auto (source: state-file"'
ok "#2011: status shows the actionable migration advisory" \
  'printf "%s" "$out" | grep -q "^advisory: .*set-mode review"'
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" set-mode review 2>&1)"; rc=$?
ok "#2011: set-mode review migrates an auto node" \
  '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "auto -> approve" && [ "$(cat "$STATE12/skill-autosave.mode")" = approve ]'
ok "#2011: set-mode writes the mode file owner-only" \
  '[ "$(stat -c %a "$STATE12/skill-autosave.mode")" = 600 ]'
ok "#2011: set-mode logs the transition" \
  'grep -q "set-mode from=auto to=approve" "$STATE12/skill-autosave.log"'
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" status 2>&1)"
ok "#2011: migrated node reports the human gate without advisory" \
  'printf "%s" "$out" | grep -q "^mode: approve" && ! printf "%s" "$out" | grep -q "^advisory:"'
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" set-mode review 2>&1)"; rc=$?
ok "#2011: set-mode is idempotent" \
  '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "unchanged" && [ "$(grep -c "set-mode from=" "$STATE12/skill-autosave.log")" = 1 ]'
out="$(CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" set-mode yolo 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "#2011: set-mode rejects an unknown mode and leaves the file alone" \
  '[ "$rc" = 2 ] && [ "$(cat "$STATE12/skill-autosave.mode")" = approve ]'
out="$(CCC_STATE_DIR="$STATE12" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTOSAVE" set-mode review 2>&1)"
ok "#2011: set-mode warns when an env override would mask the change" \
  'printf "%s" "$out" | grep -q "WARNING env CCC_SKILL_AUTOSAVE_MODE=auto overrides"'
touch "$STATE12/skill-autosave.disabled"
rm -f "$STATE12/skill-autosave.mode"
CCC_STATE_DIR="$STATE12" bash "$AUTOSAVE" set-mode review >/dev/null 2>&1
ok "#2011: set-mode works while the sweep is paused" \
  '[ "$(cat "$STATE12/skill-autosave.mode" 2>/dev/null)" = approve ]'
rm -f "$STATE12/skill-autosave.disabled"

# --- 13) #2011: curator runs by default, mark-only unless archive is opted in --
CURATOR_STUB="$TMP/curator-stub.py"
cat > "$CURATOR_STUB" <<'PY'
#!/usr/bin/env python3
import os
import sys
from pathlib import Path
line = " ".join(sys.argv[1:]) + " archive=" + os.environ.get("CCC_SKILL_CURATOR_ARCHIVE_ENABLED", "unset")
with Path(os.environ["CURATOR_TOUCH"]).open("a", encoding="utf-8") as handle:
    handle.write(line + "\n")
print('{"ok":true,"command":"run"}')
PY
STATE13="$TMP/state13"; mkdir -p "$STATE13"; chmod 700 "$STATE13"
run13() {
  env CCC_STATE_DIR="$STATE13" CLAUDE_PROJECTS_DIR="$TMP/projects13" \
    CCC_PUSH_SPOOL="$TMP/spool13" CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" \
    SCAN_TOUCH="$TMP/scan13.touched" CCC_SKILL_PROMOTION_CMD="$PROMOTER" \
    PROMOTION_TOUCH="$TMP/promotion13.touched" CCC_SKILL_CURATOR_CMD="$CURATOR_STUB" \
    CURATOR_TOUCH="$TMP/curator13.touched" CLAUDE_SKILLS_DIR="$TMP/skills13" \
    CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 CCC_NODE=testnode "$@" bash "$AUTOSAVE" run
}
run13
ok "#2011: the sweep runs the curator by default" 'grep -qx "run --auto archive=unset" "$TMP/curator13.touched"'
ok "#2011: the curator summary is logged" 'grep -q "curator {\"ok\":true" "$STATE13/skill-autosave.log"'
: > "$TMP/curator13.touched"
run13 CCC_SKILL_CURATOR_ENABLED=false
ok "#2011: CCC_SKILL_CURATOR_ENABLED=false skips the curator" '[ ! -s "$TMP/curator13.touched" ]'
ok "#2011: the curator off-switch is logged" 'grep -q "curator skipped reason=disabled" "$STATE13/skill-autosave.log"'
run13 CCC_AUTONOMY=dry-run
ok "#2011: autonomy dry-run keeps the curator report-only" 'grep -qx "run --auto --dry-run archive=unset" "$TMP/curator13.touched"'
out="$(CCC_STATE_DIR="$STATE13" bash "$AUTOSAVE" status 2>&1)"
ok "#2011: status reports curator enabled with archive moves off" \
  'printf "%s" "$out" | grep -q "^curator: enabled, archive moves off"'
out="$(CCC_STATE_DIR="$STATE13" CCC_SKILL_CURATOR_ARCHIVE_ENABLED=true bash "$AUTOSAVE" status 2>&1)"
ok "#2011: status makes an archive opt-in visible" \
  'printf "%s" "$out" | grep -q "^curator: enabled, archive moves ENABLED"'
# shellcheck disable=SC2034  # read via eval inside ok()
out="$(CCC_STATE_DIR="$STATE13" CCC_SKILL_CURATOR_ENABLED=off bash "$AUTOSAVE" status 2>&1)"
ok "#2011: status reports a disabled curator" 'printf "%s" "$out" | grep -q "^curator: disabled"'

# --- 14) #1932: unreviewable one-turn transcripts do not consume the budget ---
# Four one-turn `claude -p` batch transcripts are the newest files; the real
# multi-turn session is older. Before #1932 the three newest skips filled
# MAX_SESSIONS=3 and the real session was never drafted.
STATE14="$TMP/state14"; PROJECTS14="$TMP/projects14"
mkdir -p "$STATE14"; chmod 700 "$STATE14"
make_transcript "$PROJECTS14/-root/real-work.jsonl" 6
touch -d '-2 hours' "$PROJECTS14/-root/real-work.jsonl"
for n in 1 2 3 4; do
  make_transcript "$PROJECTS14/-root/batch-$n.jsonl" 1
  touch -d "-$((5 - n)) minutes" "$PROJECTS14/-root/batch-$n.jsonl"
done
run14() {
  env CCC_STATE_DIR="$STATE14" CLAUDE_PROJECTS_DIR="$PROJECTS14" CCC_PUSH_SPOOL="$TMP/spool14" \
    CCC_SKILL_REVIEW_CMD="$REVIEW" CCC_SKILL_SCAN_CMD="$SCAN" SCAN_TOUCH="$TMP/scan14.touched" \
    CCC_SKILL_PROMOTION_CMD="$PROMOTER" PROMOTION_TOUCH="$TMP/promotion14.touched" \
    CLAUDE_SKILLS_DIR="$TMP/skills14" CCC_SKILL_AUTOSAVE_SETTLE_SECONDS=15 \
    CCC_SKILL_AUTOSAVE_MAX_SESSIONS=3 CCC_NODE=testnode bash "$AUTOSAVE" run
}
out="$(CCC_STATE_DIR="$STATE14" bash "$AUTOSAVE" status 2>&1)"
ok "#1932: status shows no drafting review yet" 'printf "%s" "$out" | grep -q "^last drafting review: never"'
run14
ok "#1932: the older real session is drafted past the batch transcripts" \
  'grep -q "review ok session=real-work " "$STATE14/skill-autosave.log"'
ok "#1932: every one-turn transcript is skipped, not drafted" \
  '[ "$(grep -c "review skipped session=batch-[1-4] .*reason=not-reviewable" "$STATE14/skill-autosave.log")" = 4 ] && ! grep -q "review ok session=batch-" "$STATE14/skill-autosave.log"'
ok "#1932: sweep summary separates drafts from skips" \
  'grep -q "sweep done drafted_sessions=1 .*skipped_unreviewable=4" "$STATE14/skill-autosave.log"'
ok "#1932: skipped transcripts are ledgered so they are not revisited" \
  '[ "$(grep -c "^batch-[1-4]	" "$STATE14/skill-autosave.seen")" = 4 ]'
for _ in $(seq 1 40); do [ -f "$STATE14/skill-review-last.json" ] && break; sleep 0.25; done
out="$(CCC_STATE_DIR="$STATE14" bash "$AUTOSAVE" status 2>&1)"
ok "#1932: status reports the last real drafting review" \
  'printf "%s" "$out" | grep -Eq "^last drafting review: [0-9]{4}-[0-9]{2}-[0-9]{2}T"'
: > "$STATE14/skill-autosave.log"
run14
ok "#1932: a rerun revisits neither skips nor drafts" \
  '! grep -Eq "review (ok|skipped) session=" "$STATE14/skill-autosave.log"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
