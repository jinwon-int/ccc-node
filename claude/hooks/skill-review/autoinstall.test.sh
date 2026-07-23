#!/usr/bin/env bash
# Tests for skill-review/autoinstall.sh (#355) — hermetic, deterministic,
# no provider/network calls (the gates are pure shell/jq).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUTO="$HERE/autoinstall.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

STATE="$TMP/state"
SKILLS="$TMP/skills"
SPOOL="$TMP/spool"
PENDING="$STATE/pending-skills"
mkdir -p "$STATE" "$SKILLS" "$PENDING"

run_auto() { # [extra env assignments...] verb [args...]
  CCC_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_PUSH_SPOOL="$SPOOL" \
  CCC_NODE=testnode "$@"
}

make_draft() { # <id> <name> <description> [body]
  local id="$1" name="$2" desc="$3" body="${4:-}"
  mkdir -p "$PENDING/$id"
  if [ -z "$body" ]; then
    body="# ${name}

## When to Use
- Recurring procedure.

## Procedure
1. Run the checked steps.
2. Verify the output.

## Safety
- Read credentials from the env file location only.

## Verification
- Confirm the recorded output."
  fi
  printf -- '---\nname: %s\ndescription: %s\n---\n\n%s\n' "$name" "$desc" "$body" \
    > "$PENDING/$id/SKILL.md"
  jq -nc --arg id "$id" --arg name "$name" \
    '{id:$id, name:$name, status:"pending", session_id:"sess-test"}' \
    > "$PENDING/$id/meta.json"
}

# --- 1) approve mode (default): run is a strict no-op --------------------------
make_draft 20260101-000000-a-clean-one clean-one "Capture the recurring release verification checklist procedure."
out="$(run_auto bash "$AUTO" run)"
ok "approve mode reports skipped" 'jq -e ".skipped == \"mode\"" >/dev/null <<<"$out"'
ok "approve mode installs nothing" '[ ! -e "$SKILLS/clean-one" ]'
ok "approve mode leaves draft pending" '[ -d "$PENDING/20260101-000000-a-clean-one" ]'
ok "approve mode writes no ledger" '[ ! -s "$STATE/skill-autosave-install.jsonl" ]'

# --- 2) auto mode: clean draft is installed + ledgered + notified ---------------
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_TRIGGER=test bash "$AUTO" run)"
ok "clean draft installed" '[ -f "$SKILLS/clean-one/SKILL.md" ]'
ok "install marker written in skill dir" 'jq -e ".installed_by == \"autosave\"" "$SKILLS/clean-one/.autosave-meta.json" >/dev/null'
ok "ledger records installed-by=autosave" 'jq -e "select(.event==\"install\") | .installed_by == \"autosave\" and .name == \"clean-one\" and .trigger == \"test\"" "$STATE/skill-autosave-install.jsonl" >/dev/null'
ok "draft archived as installed" 'ls -d "$PENDING/20260101-000000-a-clean-one.installed-"* >/dev/null 2>&1'
ok "summary lists installed name" 'jq -e ".installed == [\"clean-one\"]" >/dev/null <<<"$out"'
ok "post-hoc notification queued" 'ls "$SPOOL"/*SkillAutoInstall*.json >/dev/null 2>&1'
ok "notification is a notice, not an approval request" 'jq -r ".text" "$SPOOL"/*SkillAutoInstall*.json | grep -q "자동 설치 1건"'
ok "notification carries dedup key" 'jq -r ".dedup" "$SPOOL"/*SkillAutoInstall*.json | grep -q "SkillAutoInstall:clean-one"'

# mode via state file (no env) behaves the same
printf 'auto\n' > "$STATE/skill-autosave.mode"
make_draft 20260101-000001-b-mode-file mode-file-skill "Summarize the recurring dependency upgrade triage workflow for the node."
out="$(run_auto bash "$AUTO" run)"
ok "mode state file enables auto" '[ -f "$SKILLS/mode-file-skill/SKILL.md" ]'
rm -f "$STATE/skill-autosave.mode"

# --- 3) secret drafts are blocked and stay pending ------------------------------
: > "$STATE/skill-autosave-install.jsonl"
find "$SPOOL" -type f -delete 2>/dev/null
make_draft 20260101-000002-c-leaky leaky-skill "Automate the recurring token rotation procedure for the deploy pipeline." \
"# Leaky

## Procedure
1. export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345
2. Run the deploy.
3. Check output.
4. Confirm.
5. Done."
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "secret draft not installed" '[ ! -e "$SKILLS/leaky-skill" ]'
ok "secret draft stays pending" '[ -d "$PENDING/20260101-000002-c-leaky" ]'
ok "block marker names pattern class only" 'jq -e ".reason == \"secret gh-token\"" "$PENDING/20260101-000002-c-leaky/autosave-block.json" >/dev/null'
ok "block marker never quotes the secret" '! grep -q ghp_ "$PENDING/20260101-000002-c-leaky/autosave-block.json"'
ok "summary counts newly blocked" 'jq -e ".newly_blocked | length == 1" >/dev/null <<<"$out"'
ok "block notification queued" 'jq -r ".text" "$SPOOL"/*SkillAutoInstall*.json | grep -q "차단 1건"'

# second run: same block is not "new" — no duplicate notification
spool_before="$(ls "$SPOOL" | wc -l | tr -d '[:space:]')"
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
spool_after="$(ls "$SPOOL" | wc -l | tr -d '[:space:]')"
ok "still-blocked draft is not re-notified" '[ "$spool_before" = "$spool_after" ]'
ok "still-blocked draft reported but not newly" 'jq -e "(.blocked | length == 1) and (.newly_blocked | length == 0)" >/dev/null <<<"$out"'

# --- 4) node-specific facts are blocked -----------------------------------------
make_draft 20260101-000003-d-nodefact node-fact-skill "Capture the recurring log inspection procedure used across sessions." \
"# Node fact

## Procedure
1. Read /home/alice/notes/checklist.md for the steps.
2. Run the inspection.
3. Verify results.
4. Record them.
5. Done."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "home-path draft blocked" '[ ! -e "$SKILLS/node-fact-skill" ] && jq -e ".reason | startswith(\"node-specific\")" "$PENDING/20260101-000003-d-nodefact/autosave-block.json" >/dev/null'

make_draft 20260101-000004-e-ip ip-skill "Document the recurring service health check flow for operators here." \
"# IP

## Procedure
1. curl http://203.0.113.7:8080/health and confirm the response.
2. Check the logs.
3. Verify status.
4. Record.
5. Done."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "non-loopback IP blocked" 'jq -e ".reason == \"node-specific ipv4\"" "$PENDING/20260101-000004-e-ip/autosave-block.json" >/dev/null'

make_draft 20260101-000005-f-local localhost-ok-skill "Verify the recurring local bridge smoke test procedure end to end." \
"# Localhost is node-agnostic

## Procedure
1. curl http://127.0.0.1:8080/health and confirm the response.
2. Check the logs for errors.
3. Verify the status output.
4. Record the result.
5. Done."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "loopback IP is allowed" '[ -f "$SKILLS/localhost-ok-skill/SKILL.md" ]'

# --- 5) lint gate ----------------------------------------------------------------
make_draft 20260101-000006-g-badname Bad_Name "Capture the recurring formatting cleanup procedure for the repository."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "non-kebab name blocked" 'jq -e ".reason == \"lint name-not-kebab\"" "$PENDING/20260101-000006-g-badname/autosave-block.json" >/dev/null'

make_draft 20260101-000007-h-shortdesc short-desc-skill "Too short."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "short description blocked" 'jq -e ".reason == \"lint description-too-short\"" "$PENDING/20260101-000007-h-shortdesc/autosave-block.json" >/dev/null'

mkdir -p "$PENDING/20260101-000008-i-nofm"
printf '# no frontmatter\njust text\n' > "$PENDING/20260101-000008-i-nofm/SKILL.md"
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "missing frontmatter blocked" 'jq -e ".reason == \"lint no-frontmatter\"" "$PENDING/20260101-000008-i-nofm/autosave-block.json" >/dev/null'

# --- 6) dedup gate ----------------------------------------------------------------
mkdir -p "$SKILLS/existing-skill"
printf -- '---\nname: existing-skill\ndescription: Run the recurring wiki record procedure for durable decisions.\n---\n\n# Existing\n' \
  > "$SKILLS/existing-skill/SKILL.md"
make_draft 20260101-000009-j-dupname existing-skill "Another take on the wiki record procedure with different wording entirely."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "existing name blocked (never overwrite)" 'jq -e ".reason | startswith(\"dedup already-exists\")" "$PENDING/20260101-000009-j-dupname/autosave-block.json" >/dev/null'

make_draft 20260101-000010-k-dupdesc wiki-recorder "Run the recurring wiki record procedure for durable decisions."
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "similar description blocked" 'jq -e ".reason | startswith(\"dedup description-similar\")" "$PENDING/20260101-000010-k-dupdesc/autosave-block.json" >/dev/null'

# --- 7) daily cap defers (not blocks) ----------------------------------------------
CAP_STATE="$TMP/capstate"; CAP_SKILLS="$TMP/capskills"; CAP_SPOOL="$TMP/capspool"
mkdir -p "$CAP_STATE/pending-skills" "$CAP_SKILLS"
PENDING_SAVE="$PENDING"; STATE_SAVE="$STATE"; SKILLS_SAVE="$SKILLS"
STATE="$CAP_STATE"; SKILLS="$CAP_SKILLS"; PENDING="$CAP_STATE/pending-skills"
make_draft 20260101-000011-l-cap1 cap-one "Capture the first recurring maintenance procedure for the fleet nodes."
make_draft 20260101-000012-m-cap2 cap-two "Capture the second recurring maintenance procedure for backup checks."
out="$(CCC_STATE_DIR="$CAP_STATE" CLAUDE_SKILLS_DIR="$CAP_SKILLS" CCC_PUSH_SPOOL="$CAP_SPOOL" \
  CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=1 bash "$AUTO" run)"
ok "cap installs only one" '[ "$(find "$CAP_SKILLS" -name SKILL.md | wc -l | tr -d "[:space:]")" = 1 ]'
ok "over-cap draft deferred, not blocked" 'jq -e ".deferred == 1" >/dev/null <<<"$out" && ! ls "$CAP_STATE/pending-skills"/*/autosave-block.json >/dev/null 2>&1'
ok "cap counts prior installs from ledger" '[ "$(CCC_STATE_DIR="$CAP_STATE" CLAUDE_SKILLS_DIR="$CAP_SKILLS" CCC_PUSH_SPOOL="$CAP_SPOOL" CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=1 bash "$AUTO" run | jq -r ".installed | length")" = 0 ]'
STATE="$STATE_SAVE"; SKILLS="$SKILLS_SAVE"; PENDING="$PENDING_SAVE"

# --- 8) off-switch wins over auto mode ----------------------------------------------
touch "$STATE/skill-autosave.disabled"
make_draft 20260101-000013-n-off off-switch-skill "Capture the recurring certificate renewal check procedure for services."
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "off-switch skips auto install" 'jq -e ".skipped == \"disabled\"" >/dev/null <<<"$out" && [ ! -e "$SKILLS/off-switch-skill" ]'
rm -f "$STATE/skill-autosave.disabled"
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null

# --- 9) list + rollback ---------------------------------------------------------------
out="$(run_auto bash "$AUTO" list)"
ok "list shows installed skill" 'grep -q "clean-one" <<<"$out"'
ok "list shows blocked drafts" 'grep -q "reason=secret gh-token" <<<"$out"'

out="$(run_auto bash "$AUTO" rollback clean-one)"
ok "rollback removes the skill" '[ ! -e "$SKILLS/clean-one" ]'
ok "rollback archives, not deletes" 'ls -d "$STATE/skill-autosave-rollback/clean-one."* >/dev/null 2>&1'
ok "rollback appends ledger event" 'jq -e "select(.event==\"rollback\") | .name == \"clean-one\"" "$STATE/skill-autosave-install.jsonl" >/dev/null'

mkdir -p "$SKILLS/hand-made"
printf -- '---\nname: hand-made\ndescription: Operator-authored skill that autosave must never touch at all.\n---\n\n# Hand\n' \
  > "$SKILLS/hand-made/SKILL.md"
run_auto bash "$AUTO" rollback hand-made >/dev/null 2>&1; rc=$?
ok "rollback refuses non-autosave skill" '[ "$rc" != 0 ] && [ -f "$SKILLS/hand-made/SKILL.md" ]'

run_auto bash "$AUTO" rollback --all >/dev/null 2>&1
ok "rollback --all clears autosave installs" '! find "$SKILLS" -name .autosave-meta.json | grep -q .'
ok "rollback --all leaves hand-made skill" '[ -f "$SKILLS/hand-made/SKILL.md" ]'

# --- 10) status is read-only ------------------------------------------------------------
out="$(run_auto bash "$AUTO" status)"
ok "status reports mode and cap" 'grep -q "^mode: approve" <<<"$out" && grep -q "daily cap:" <<<"$out"'

# --- 11) fleet autonomy guard (#386): kill + dry-run over auto mode ------------
A_STATE="$TMP/autonomy-state"; A_SKILLS="$TMP/autonomy-skills"
mkdir -p "$A_STATE/pending-skills" "$A_SKILLS"
make_draft_at() { # <store> <skills> <id> <name> <desc>
  local st="$1" sk="$2" id="$3" nm="$4" desc="$5"
  mkdir -p "$st/pending-skills/$id"
  printf -- '---\nname: %s\ndescription: %s\n---\n\n# %s\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n' "$nm" "$desc" "$nm" > "$st/pending-skills/$id/SKILL.md"
  jq -nc --arg id "$id" --arg name "$nm" '{id:$id,name:$name,status:"pending",session_id:"s"}' > "$st/pending-skills/$id/meta.json"
}

# CCC_AUTONOMY=kill halts autonomous install regardless of auto mode.
make_draft_at "$A_STATE" "$A_SKILLS" a-kill kill-me "Capture the recurring autonomy kill-switch verification procedure now."
out="$(CCC_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_AUTONOMY=kill CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "CCC_AUTONOMY=kill installs nothing" 'jq -e ".skipped == \"autonomy-kill\"" >/dev/null <<<"$out" && [ ! -e "$A_SKILLS/kill-me" ]'

# CCC_AUTONOMY=dry-run gates + reports would_install but writes nothing.
out="$(CCC_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_AUTONOMY=dry-run CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "CCC_AUTONOMY=dry-run reports would_install, writes nothing" 'jq -e ".dry_run == true and (.would_install | index(\"kill-me\") != null) and (.installed | length == 0)" >/dev/null <<<"$out" && [ ! -e "$A_SKILLS/kill-me" ] && [ ! -s "$A_STATE/skill-autosave-install.jsonl" ]'

# File switch: autonomy.dry-run in the state dir.
touch "$A_STATE/autonomy.dry-run"
out="$(CCC_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "autonomy.dry-run file mutes install" 'jq -e ".dry_run == true and (.installed | length == 0)" >/dev/null <<<"$out" && [ ! -e "$A_SKILLS/kill-me" ]'
rm -f "$A_STATE/autonomy.dry-run"

# active (default) still installs.
out="$(CCC_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "default autonomy=active installs" '[ -f "$A_SKILLS/kill-me/SKILL.md" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
