#!/usr/bin/env bash
# Tests for skill-review/autoinstall.sh (#355) — hermetic, deterministic,
# no provider/network calls (the gates are pure shell/jq).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUTO="$HERE/autoinstall.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
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

STATE="$TMP/state"
SKILLS="$TMP/skills"
SPOOL="$TMP/spool"
PENDING="$STATE/pending-skills"
mkdir -p "$STATE" "$SKILLS" "$PENDING"
chmod 700 "$STATE"
# The ownership contract fail-closes on group/other-writable skills roots, so
# fixtures must model a compliant root under any umask (#770).
chmod 700 "$SKILLS"

run_auto() { # [extra env assignments...] verb [args...]
  CCC_SKILL_REVIEW_STATE_DIR="$STATE" CLAUDE_SKILLS_DIR="$SKILLS" CCC_PUSH_SPOOL="$SPOOL" \
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
ok "v2 install marker written owner-only" 'jq -e ".schema_version == 2 and .installed_by == \"autosave\" and .created_by == \"ccc-node\" and .rollback_eligible == true" "$SKILLS/clean-one/.autosave-meta.json" >/dev/null && [ "$(stat -c %a "$SKILLS/clean-one/.autosave-meta.json")" = 600 ]'
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

# Provenance failure removes only the exclusively-created install directory,
# leaves the draft pending, and never publishes an install ledger row.
make_draft 20260101-000001-c-provenance provenance-fail "Capture the recurring provenance failure recovery procedure."
mkdir -p "$TMP/fail-bin"
cat > "$TMP/fail-bin/python3" <<'STUB'
#!/bin/sh
printf '%s\n' '{"ok": false, "code": "stubbed_unsafe_skills_root"}'
exit 2
STUB
chmod +x "$TMP/fail-bin/python3"
out="$(run_auto env PATH="$TMP/fail-bin:$PATH" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "provenance failure leaves no partial install directory" '[ ! -e "$SKILLS/provenance-fail" ]'
ok "provenance failure leaves draft pending and unledgered" '[ -d "$PENDING/20260101-000001-c-provenance" ] && ! jq -e "select(.event == \"install\" and .name == \"provenance-fail\")" "$STATE/skill-autosave-install.jsonl" >/dev/null'
ok "provenance cleanup result is explicit" 'grep -q "name=provenance-fail reason=provenance-write detail=stubbed_unsafe_skills_root cleanup=complete" "$STATE/skill-autoinstall.log"'
rm -rf "$PENDING/20260101-000001-c-provenance"

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

# --- 3b) the api-key pattern must not match "sk-" inside an ordinary word ------
# Measured on a live node: the unanchored pattern matched 2 drafts and both were
# false positives, each on its own name — "di|sk-usage-diagnosis-and-planning"
# and "ri|sk-driven-lane-composition". A 100% false-positive rate on that
# corpus, and the block is terminal: the draft never installs, and one of the
# two had already been judged worth keeping.
make_draft 20260101-000009-a-riskword disk-usage-diagnosis-and-planning \
  "Diagnose the recurring disk-usage growth and rank risk-driven cleanup options."
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "word containing sk- is not blocked as an api key" \
  '[ ! -f "$PENDING/20260101-000009-a-riskword/autosave-block.json" ]'
ok "draft named with sk- inside a word installs" \
  '[ -e "$SKILLS/disk-usage-diagnosis-and-planning" ]'

# ...while a real key in the same position is still caught. Widening a secret
# pattern is a loosening, so pin the detection it must keep.
make_draft 20260101-000010-a-realkey real-key-skill \
  "Automate the recurring provider key rotation procedure for the pipeline." \
"# Real key

## Procedure
1. export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345
2. Run the job.
3. Check output.
4. Confirm.
5. Done."
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "real api key is still blocked" \
  'jq -e ".reason == \"secret api-key\"" "$PENDING/20260101-000010-a-realkey/autosave-block.json" >/dev/null'
ok "real api key draft not installed" '[ ! -e "$SKILLS/real-key-skill" ]'
ok "api-key block marker never quotes the secret" \
  '! grep -q "sk-abcdefghij" "$PENDING/20260101-000010-a-realkey/autosave-block.json"'

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

# --- 5b) size gate (#1347 rubric: progressive disclosure) ------------------------
# 501 non-empty lines exceed the official <500-line guidance: the draft is
# blocked with an oversized-body reason and must be split by the author.
big="$PENDING/20260101-000009-j-bigbody"
mkdir -p "$big"
{
  printf -- '---\nname: big-body-skill\ndescription: Exercise the progressive disclosure size gate with an oversized body.\n---\n\n# Big\n\n'
  seq -f 'Filler line %g.' 1 501
} > "$big/SKILL.md"
big_out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run 2>&1)"
ok "oversized body blocked" 'jq -e ".reason | startswith(\"size oversized-body\")" "$PENDING/20260101-000009-j-bigbody/autosave-block.json" >/dev/null'
# #1399: this assertion failed once on CI (2026-09-02) with no reproduction in
# 7 local runs, then again in the CI run of the diagnostics PR itself. The
# harness runner keeps only the tail of a failing suite, so the dump is three
# compact lines with the run summary LAST (it carries "skipped": locked /
# daily-cap / incremental-usage-unavailable — the likeliest common cause of
# both this and the 500-line edge case failing in the same run).
diag_1399() { # <draft-dir> <label> <run-output>
  echo "#1399[$2] dir=$(ls -d "$1"* 2>/dev/null | tr '\n' ' ') block=$(cat "$1/autosave-block.json" 2>/dev/null | head -c 200) lines=$(wc -l < "$1/SKILL.md" 2>/dev/null) installs_today=$(grep -c '"event":"install"' "$STATE/skill-autosave-install.jsonl" 2>/dev/null)"
  echo "#1399[$2] log: $(tail -n 4 "$STATE/skill-autoinstall.log" 2>/dev/null | tr '\n' '|' | head -c 400)"
  echo "#1399[$2] run: $(printf '%s' "$3" | tr '\n' ' ' | head -c 400)"
}
if ! jq -e ".reason | startswith(\"size oversized-body\")" "$PENDING/20260101-000009-j-bigbody/autosave-block.json" >/dev/null 2>&1; then
  diag_1399 "$PENDING/20260101-000009-j-bigbody" bigbody "$big_out"
fi

# 500 lines is exactly at the official limit and must pass the size gate.
edge="$PENDING/20260101-000010-k-edgebody"
mkdir -p "$edge"
{
  printf -- '---\nname: edge-body-skill\ndescription: Sit exactly at the progressive disclosure limit and stay installable.\n---\n\n# Edge\n\n'
  seq -f 'Filler line %g.' 1 493
} > "$edge/SKILL.md"
edge_out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run 2>&1)"
ok "body at the 500-line limit installs" '[ -f "$SKILLS/edge-body-skill/SKILL.md" ]'
if [ ! -f "$SKILLS/edge-body-skill/SKILL.md" ]; then
  diag_1399 "$PENDING/20260101-000010-k-edgebody" edgebody "$edge_out"
fi

# --- 5b-ii) #1399: a body larger than the pipe buffer must still see its heading.
# gate_lint used `awk | grep -q`; grep exits on the first heading, awk then dies
# of EPIPE writing the rest, and pipefail turned that into "lint no-headings".
# 493 lines × ~200 bytes ≈ 100 KB (> 64 KB pipe buffer) reproduces it every
# time on a pipe; the awk-only check is immune.
wide="$PENDING/20260101-000011-l-widebody"
mkdir -p "$wide"
{
  printf -- '---\nname: wide-body-skill\ndescription: Long lines below the limit must not trip the heading check.\n---\n\n# Wide\n\n'
  seq -f 'Filler line %g. abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz' 1 493
} > "$wide/SKILL.md"
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=99 bash "$AUTO" run >/dev/null
ok "body larger than the pipe buffer still passes the heading lint (#1399)" \
  '[ -f "$SKILLS/wide-body-skill/SKILL.md" ] && [ ! -f "$wide/autosave-block.json" ]'

# Deterministic form of the same race: ~1 MB after the heading fails 20/20 on
# the old pipeline (measured on mawk 1.3.4). Such a body is over the size limit,
# so the CORRECT verdict is the size gate; the pre-fix verdict was the lint gate
# lying about the heading, which runs first.
huge="$PENDING/20260101-000012-m-hugebody"
mkdir -p "$huge"
{
  printf -- '---\nname: huge-body-skill\ndescription: A megabyte of body must be judged by the size gate, not misread as headingless.\n---\n\n# Huge\n\n'
  seq -f 'Filler line %g. abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz' 1 5000
} > "$huge/SKILL.md"
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=99 bash "$AUTO" run >/dev/null
ok "megabyte body is blocked by the size gate, not by a false no-headings (#1399)" \
  'jq -e ".reason | startswith(\"size oversized-body\")" "$huge/autosave-block.json" >/dev/null'

# --- 5c) compatibility field lint (official spec: <=500 chars) -------------------
long_compat="$PENDING/20260101-000011-l-longcompat"
mkdir -p "$long_compat"
{
  printf -- '---\nname: long-compat-skill\n'
  printf 'compatibility: %s\n' "$(printf 'Requires %s ' $(seq 1 120))"
  printf -- 'description: Exercise the optional compatibility field length lint.\n---\n\n# Compat\n\n1. Step.\n2. Step.\n3. Step.\n'
} > "$long_compat/SKILL.md"
run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "over-long compatibility field blocked" 'jq -e ".reason == \"lint compatibility-too-long\"" "$PENDING/20260101-000011-l-longcompat/autosave-block.json" >/dev/null'

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
chmod 700 "$CAP_STATE"
chmod 700 "$CAP_SKILLS"  # contract-compliant root under any umask (#770)
PENDING_SAVE="$PENDING"; STATE_SAVE="$STATE"; SKILLS_SAVE="$SKILLS"
STATE="$CAP_STATE"; SKILLS="$CAP_SKILLS"; PENDING="$CAP_STATE/pending-skills"
make_draft 20260101-000011-l-cap1 cap-one "Capture the first recurring maintenance procedure for the fleet nodes."
make_draft 20260101-000012-m-cap2 cap-two "Capture the second recurring maintenance procedure for backup checks."
out="$(CCC_SKILL_REVIEW_STATE_DIR="$CAP_STATE" CLAUDE_SKILLS_DIR="$CAP_SKILLS" CCC_PUSH_SPOOL="$CAP_SPOOL" \
  CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=1 bash "$AUTO" run)"
ok "cap installs only one" '[ "$(find "$CAP_SKILLS" -name SKILL.md | wc -l | tr -d "[:space:]")" = 1 ]'
ok "over-cap draft deferred, not blocked" 'jq -e ".deferred == 1" >/dev/null <<<"$out" && ! ls "$CAP_STATE/pending-skills"/*/autosave-block.json >/dev/null 2>&1'
ok "cap counts prior installs from ledger" '[ "$(CCC_SKILL_REVIEW_STATE_DIR="$CAP_STATE" CLAUDE_SKILLS_DIR="$CAP_SKILLS" CCC_PUSH_SPOOL="$CAP_SPOOL" CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=1 bash "$AUTO" run | jq -r ".installed | length")" = 0 ]'
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
# Contract-compliant perms regardless of the runner umask (#770).
chmod 700 "$SKILLS/hand-made"
chmod 600 "$SKILLS/hand-made/SKILL.md"
run_auto bash "$AUTO" rollback hand-made >/dev/null 2>&1; rc=$?
ok "rollback refuses non-autosave skill" '[ "$rc" != 0 ] && [ -f "$SKILLS/hand-made/SKILL.md" ]'

run_auto bash "$AUTO" rollback --all >/dev/null 2>&1
ok "rollback --all clears autosave installs" '! find "$SKILLS" -name .autosave-meta.json | grep -q .'
ok "rollback --all leaves hand-made skill" '[ -f "$SKILLS/hand-made/SKILL.md" ]'

out="$(run_auto bash "$AUTO" adopt hand-made --dry-run)"
ok "autoinstall proxy exposes adopt dry-run" 'jq -e ".dry_run == true and .reason == \"would-adopt\"" >/dev/null <<<"$out"'
run_auto bash "$AUTO" adopt hand-made >/dev/null
run_auto bash "$AUTO" rollback hand-made >/dev/null 2>&1; rc=$?
ok "adopted hand-made skill cannot be rolled back" '[ "$rc" != 0 ] && [ -f "$SKILLS/hand-made/SKILL.md" ] && jq -e ".created_by == \"operator-adopt\" and .rollback_eligible == false" "$SKILLS/hand-made/.autosave-meta.json" >/dev/null'

# --- 10) status is read-only ------------------------------------------------------------
out="$(run_auto bash "$AUTO" status)"
ok "status reports mode and cap" 'grep -q "^mode: approve" <<<"$out" && grep -q "daily cap:" <<<"$out"'

# --- 11) fleet autonomy guard (#386): kill + dry-run over auto mode ------------
A_STATE="$TMP/autonomy-state"; A_SKILLS="$TMP/autonomy-skills"
mkdir -p "$A_STATE/pending-skills" "$A_SKILLS"
chmod 700 "$A_STATE"
chmod 700 "$A_SKILLS"  # contract-compliant root under any umask (#770)
make_draft_at() { # <store> <skills> <id> <name> <desc>
  local st="$1" sk="$2" id="$3" nm="$4" desc="$5"
  mkdir -p "$st/pending-skills/$id"
  printf -- '---\nname: %s\ndescription: %s\n---\n\n# %s\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n' "$nm" "$desc" "$nm" > "$st/pending-skills/$id/SKILL.md"
  jq -nc --arg id "$id" --arg name "$nm" '{id:$id,name:$name,status:"pending",session_id:"s"}' > "$st/pending-skills/$id/meta.json"
}

# CCC_AUTONOMY=kill halts autonomous install regardless of auto mode.
make_draft_at "$A_STATE" "$A_SKILLS" a-kill kill-me "Capture the recurring autonomy kill-switch verification procedure now."
out="$(CCC_SKILL_REVIEW_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_AUTONOMY=kill CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "CCC_AUTONOMY=kill installs nothing" 'jq -e ".skipped == \"autonomy-kill\"" >/dev/null <<<"$out" && [ ! -e "$A_SKILLS/kill-me" ]'

# CCC_AUTONOMY=dry-run gates + reports would_install but writes nothing.
out="$(CCC_SKILL_REVIEW_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_AUTONOMY=dry-run CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "CCC_AUTONOMY=dry-run reports would_install, writes nothing" 'jq -e ".dry_run == true and (.would_install | index(\"kill-me\") != null) and (.installed | length == 0)" >/dev/null <<<"$out" && [ ! -e "$A_SKILLS/kill-me" ] && [ ! -s "$A_STATE/skill-autosave-install.jsonl" ]'

# File switch: autonomy.dry-run in the state dir.
touch "$A_STATE/autonomy.dry-run"
out="$(CCC_SKILL_REVIEW_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "autonomy.dry-run file mutes install" 'jq -e ".dry_run == true and (.installed | length == 0)" >/dev/null <<<"$out" && [ ! -e "$A_SKILLS/kill-me" ]'
rm -f "$A_STATE/autonomy.dry-run"

# active (default) still installs.
out="$(CCC_SKILL_REVIEW_STATE_DIR="$A_STATE" CLAUDE_SKILLS_DIR="$A_SKILLS" CCC_PUSH_SPOOL="$TMP/aspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "default autonomy=active installs" '[ -f "$A_SKILLS/kill-me/SKILL.md" ]'

# --- failed counter (#770): a non-compliant skills root fail-closes the
# install and is reported as failed, distinct from a silent skip --------------
F_STATE="$TMP/failstate"; F_SKILLS="$TMP/failskills"
mkdir -p "$F_STATE/pending-skills" "$F_SKILLS"
chmod 700 "$F_STATE"
chmod 777 "$F_SKILLS"  # deliberately non-compliant: contract must fail closed
make_draft_at "$F_STATE" "$F_SKILLS" f-fail fail-one "Capture the recurring failed-counter verification procedure here."
out="$(CCC_SKILL_REVIEW_STATE_DIR="$F_STATE" CLAUDE_SKILLS_DIR="$F_SKILLS" CCC_PUSH_SPOOL="$TMP/fspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "non-compliant root reports failed=1, installs nothing" 'jq -e ".failed == 1 and (.installed | length == 0)" >/dev/null <<<"$out" && [ ! -e "$F_SKILLS/fail-one" ]'
ok "failed install leaves draft pending" '[ -d "$F_STATE/pending-skills/f-fail" ]'

# --- queue anchor: CCC_STATE_DIR must NOT relocate the queue -----------------
# The bridge exports CCC_STATE_DIR per memory audience while the collector
# stages drafts into the node-global ~/.claude/state/pending-skills. Honouring
# it here made `status` report an empty queue while real drafts sat unread.
Q_STATE="$TMP/qstate"; Q_SKILLS="$TMP/qskills"; Q_DECOY="$TMP/qdecoy"
mkdir -p "$Q_STATE/pending-skills" "$Q_SKILLS" "$Q_DECOY/pending-skills"
chmod 700 "$Q_STATE" "$Q_SKILLS" "$Q_DECOY"
make_draft_at "$Q_STATE" "$Q_SKILLS" q-anchor queue-anchor "Capture the recurring queue anchor verification procedure here."
out="$(CCC_SKILL_REVIEW_STATE_DIR="$Q_STATE" CCC_STATE_DIR="$Q_DECOY" CLAUDE_SKILLS_DIR="$Q_SKILLS" CCC_PUSH_SPOOL="$TMP/qspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" status)"
ok "CCC_STATE_DIR does not hide the real queue" 'grep -q "pending drafts: 1" <<<"$out"'
out="$(CCC_SKILL_REVIEW_STATE_DIR="$Q_STATE" CCC_STATE_DIR="$Q_DECOY" CLAUDE_SKILLS_DIR="$Q_SKILLS" CCC_PUSH_SPOOL="$TMP/qspool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "CCC_STATE_DIR does not relocate installs" 'jq -e "(.installed | index(\"queue-anchor\")) != null" >/dev/null <<<"$out" && [ -f "$Q_SKILLS/queue-anchor/SKILL.md" ]'
ok "CCC_STATE_DIR decoy queue stays untouched" '[ -z "$(ls -A "$Q_DECOY/pending-skills")" ] && [ ! -e "$Q_DECOY/skill-autosave-install.jsonl" ]'

# --- 12) unverified factual claims are blocked --------------------------------
# The machine cannot check whether a claim is TRUE, only whether it is CITED.
# URL probing is disabled here so the suite stays hermetic; the dead-citation
# path is covered below through claim_urls/url_missing, which need no network.
# The daily cap is already spent by section 7, and a cap deferral is not a gate
# verdict — raise it so these cases exercise the claim gate and nothing else.
run_claims() {
  run_auto env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_GATE_URLCHECK=0 \
    CCC_SKILL_AUTOSAVE_DAILY_CAP=99 bash "$AUTO" run >/dev/null
}

make_draft 20260101-000020-a-exitcode exit-claim-skill "Diagnose the recurring watch timeout by reading the command exit status." \
"# Exit claim

## Procedure
1. Run the checks command and observe that it exits 124 immediately.
2. Query the rollup instead.
3. Wait and retry.
4. Resume the watch.
5. Merge once green."
run_claims
ok "uncited exit-code claim blocked" \
  '[ ! -e "$SKILLS/exit-claim-skill" ] && jq -e ".reason == \"unverified-claim exit-code\"" "$PENDING/20260101-000020-a-exitcode/autosave-block.json" >/dev/null'
ok "claim block marker never quotes the draft" \
  '! grep -q "124" "$PENDING/20260101-000020-a-exitcode/autosave-block.json"'

make_draft 20260101-000021-b-cited exit-cited-skill "Diagnose the recurring watch timeout using the documented command exit status." \
"# Exit claim with a source citation

## Procedure
1. The command exits 8 when checks are pending (pkg/cmd/pr/checks/checks.go:251).
2. Query the rollup instead.
3. Wait and retry.
4. Resume the watch.
5. Merge once green."
run_claims
ok "exit-code claim with source ref installs" '[ -f "$SKILLS/exit-cited-skill/SKILL.md" ]'

make_draft 20260101-000022-c-helpcited exit-help-skill "Diagnose the recurring watch failure by consulting the command help output." \
"# Exit claim backed by a runnable check

## Procedure
1. Run the command with --help to read its documented exit codes.
2. Confirm the pending code from that output.
3. Query the rollup instead.
4. Wait and retry.
5. Merge once green."
run_claims
ok "exit-code claim with --help installs" '[ -f "$SKILLS/exit-help-skill/SKILL.md" ]'

make_draft 20260101-000023-d-httpstatus http-claim-skill "Audit the recurring API failure path by classifying the returned status." \
"# HTTP claim

## Procedure
1. The endpoint answers HTTP 409 when the branch is stale.
2. Refresh the branch.
3. Retry the request.
4. Confirm the result.
5. Record it."
run_claims
ok "uncited http-status claim blocked" \
  'jq -e ".reason == \"unverified-claim http-status\"" "$PENDING/20260101-000023-d-httpstatus/autosave-block.json" >/dev/null'

make_draft 20260101-000024-e-version version-claim-skill "Pin the recurring upgrade procedure to the release that fixed the defect." \
"# Version claim

## Procedure
1. Confirm the toolchain is at least 2.96.0 before continuing.
2. Upgrade if it is older.
3. Re-run the check.
4. Confirm the result.
5. Record it."
run_claims
ok "uncited version-pin claim blocked" \
  'jq -e ".reason == \"unverified-claim version-pin\"" "$PENDING/20260101-000024-e-version/autosave-block.json" >/dev/null'

make_draft 20260101-000025-f-noclaim no-claim-skill "Capture the recurring review walkthrough that carries no factual assertions." \
"# No falsifiable claim

## Procedure
1. Read the diff end to end.
2. Note anything surprising.
3. Ask the author about it.
4. Record the outcome.
5. Close the review."
run_claims
ok "claim gate leaves claim-free drafts alone" '[ -f "$SKILLS/no-claim-skill/SKILL.md" ]'

# claim_urls / url_missing are pure text handling for these inputs — assert them
# directly rather than through a run, so no network call is needed.
# shellcheck disable=SC1090
. <(sed -n '/^claim_urls()/,/^}/p;/^url_missing()/,/^}/p' "$AUTO")
CU="$TMP/cu.md"
printf '%s\n' \
  'See https://example.org/a for details.' \
  'Also docs.github.com/en/rest/pulls covers it.' \
  'The old docs.github.com/en/repositories/configuring-branches-and-merges/ form 404s now.' \
  'Source is pkg/cmd/pr/checks/checks.go:303 and notes live in docs/notes.md.' > "$CU"
urls="$(claim_urls "$CU")"
ok "scheme-bearing URL extracted" 'grep -qx "https://example.org/a" <<<"$urls"'
ok "schemeless citation extracted and normalized" 'grep -qx "https://docs.github.com/en/rest/pulls" <<<"$urls"'
ok "URL documented as dead is not re-probed as a citation" '! grep -q "configuring-branches-and-merges/" <<<"$urls"'
ok "source ref is not mistaken for a URL" '! grep -q "checks.go" <<<"$urls"'
ok "relative doc path is not mistaken for a URL" '! grep -q "docs/notes.md" <<<"$urls"'
ok "template placeholder URL is never condemned" '! url_missing "https://github.com/OWNER/REPO/pull/NUM.diff"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
