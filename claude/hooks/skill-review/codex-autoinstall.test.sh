#!/usr/bin/env bash
# Tests for Codex-native skill-autosave install parity (#643) — hermetic,
# deterministic, no provider/network calls. Exercises autoinstall.sh with
# CCC_SKILL_PROVIDER=codex so the same gate/ledger/rollback pipeline installs
# into CODEX_HOME/skills instead of ~/.claude/skills, plus the Codex-only
# compatibility gate, the secure install-dir contract, and concurrency safety.
#
# Env-injected dirs only (no $HOME mutation, no uid assumptions), so the suite
# runs identically as root, non-root, and Termux. Where a permission bit is
# asserted it is one that holds on all three (0700 on a freshly created dir).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUTO="$HERE/autoinstall.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

STATE="$TMP/state"
CODEX_HOME_DIR="$TMP/codex"
CODEX_SKILLS="$CODEX_HOME_DIR/skills"
CLAUDE_SKILLS="$TMP/claude-skills"   # must stay empty: Codex must not touch it
SPOOL="$TMP/spool"
PENDING="$STATE/pending-skills"
mkdir -p "$STATE" "$PENDING" "$CLAUDE_SKILLS"

# Codex-provider run: note CODEX_SKILLS_DIR is the install target and
# CLAUDE_SKILLS_DIR is also set (to a separate empty dir) to prove the provider
# selects the Codex surface and never the Claude one.
run_codex() { # [extra env...] verb [args...]
  CCC_STATE_DIR="$STATE" CCC_SKILL_PROVIDER=codex \
  CODEX_SKILLS_DIR="$CODEX_SKILLS" CLAUDE_SKILLS_DIR="$CLAUDE_SKILLS" \
  CCC_PUSH_SPOOL="$SPOOL" CCC_NODE=testnode "$@"
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
    '{id:$id, name:$name, status:"pending", session_id:"sess-codex"}' \
    > "$PENDING/$id/meta.json"
}

# --- 1) approve mode (default): no install on the Codex surface ------------------
make_draft 20260101-000000-a-clean codex-clean-one "Capture the recurring Codex release verification checklist procedure."
out="$(run_codex bash "$AUTO" run)"
ok "codex approve mode reports skipped" 'jq -e ".skipped == \"mode\"" >/dev/null <<<"$out"'
ok "codex approve installs nothing" '[ ! -e "$CODEX_SKILLS/codex-clean-one" ]'
ok "codex approve leaves draft pending" '[ -d "$PENDING/20260101-000000-a-clean" ]'

# --- 2) auto mode: clean draft installs into CODEX_HOME/skills -------------------
out="$(run_codex env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_TRIGGER=test bash "$AUTO" run)"
ok "codex clean draft installed into codex skills dir" '[ -f "$CODEX_SKILLS/codex-clean-one/SKILL.md" ]'
ok "codex install never touched claude skills dir" '[ -z "$(ls -A "$CLAUDE_SKILLS" 2>/dev/null)" ]'
ok "codex install marker written" 'jq -e ".installed_by == \"autosave\"" "$CODEX_SKILLS/codex-clean-one/.autosave-meta.json" >/dev/null'
ok "codex ledger records install" 'jq -e "select(.event==\"install\") | .name == \"codex-clean-one\" and .trigger == \"test\"" "$STATE/skill-autosave-install.jsonl" >/dev/null'
ok "codex draft archived as installed" 'ls -d "$PENDING/20260101-000000-a-clean.installed-"* >/dev/null 2>&1'
ok "codex post-hoc notification queued" 'ls "$SPOOL"/*SkillAutoInstall*.json >/dev/null 2>&1'
# Freshly created codex skills dir is owner-only (holds on root/non-root/Termux).
ok "codex skills dir created 0700" '[ "$(stat -c "%a" "$CODEX_SKILLS" 2>/dev/null || stat -f "%Lp" "$CODEX_SKILLS" 2>/dev/null)" = "700" ]'

# --- 3) auto mode: installed skill is discoverable in a fresh Codex session ------
# Codex discovers personal skills at CODEX_HOME/skills/<name>/SKILL.md; a new
# session resolves the same path with valid frontmatter.
ok "installed skill discoverable by codex skills resolver" '
  . "$HERE/provider.sh";
  d="$(CODEX_SKILLS_DIR="$CODEX_SKILLS" ccc_skills_dir codex)";
  [ -f "$d/codex-clean-one/SKILL.md" ] && head -1 "$d/codex-clean-one/SKILL.md" | grep -q "^---"'

# --- 4) secret / node-specific drafts blocked (redaction-safe reason) ------------
make_draft 20260101-000001-b-leaky codex-leaky "Automate the recurring Codex token rotation procedure for the deploy pipeline." \
"# Leaky

## Procedure
1. export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345
2. Run the deploy.
3. Check output.
4. Confirm.
5. Done."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex secret draft not installed" '[ ! -e "$CODEX_SKILLS/codex-leaky" ]'
ok "codex secret block names pattern class only" 'jq -e ".reason == \"secret gh-token\"" "$PENDING/20260101-000001-b-leaky/autosave-block.json" >/dev/null'
ok "codex secret block never quotes the secret" '! grep -q ghp_ "$PENDING/20260101-000001-b-leaky/autosave-block.json"'

make_draft 20260101-000002-c-node codex-nodefact "Capture the recurring Codex log inspection procedure used across sessions." \
"# Node fact

## Procedure
1. Read /home/alice/notes/checklist.md for the steps.
2. Run the inspection.
3. Verify.
4. Record.
5. Done."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex node-specific draft blocked" '[ ! -e "$CODEX_SKILLS/codex-nodefact" ] && jq -e ".reason | startswith(\"node-specific\")" "$PENDING/20260101-000002-c-node/autosave-block.json" >/dev/null'

# --- 5) Codex-incompatible (Claude-only) drafts are isolated, not installed ------
make_draft 20260101-000003-d-cli codex-claude-cli "Document the recurring Codex review drafting procedure for operators." \
"# Uses the Claude CLI

## Procedure
1. Run claude -p to draft the summary.
2. Save it.
3. Verify.
4. Record.
5. Done."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex rejects claude -p coupling" 'jq -e ".reason == \"codex-incompat claude-cli\"" "$PENDING/20260101-000003-d-cli/autosave-block.json" >/dev/null'

make_draft 20260101-000004-e-home codex-claude-home "Document the recurring Codex state inspection procedure for the node here." \
"# Reads the Claude home tree

## Procedure
1. Inspect ~/.claude/state/pending-skills for drafts.
2. Summarize.
3. Verify.
4. Record.
5. Done."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex rejects .claude home coupling" 'jq -e ".reason == \"codex-incompat claude-home\"" "$PENDING/20260101-000004-e-home/autosave-block.json" >/dev/null'

make_draft 20260101-000005-f-env codex-claude-env "Document the recurring Codex skills directory audit procedure for the fleet." \
"# Uses CLAUDE env

## Procedure
1. Echo CLAUDE_SKILLS_DIR to find the install target.
2. Audit it.
3. Verify.
4. Record.
5. Done."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex rejects CLAUDE_ env coupling" 'jq -e ".reason == \"codex-incompat claude-env\"" "$PENDING/20260101-000005-f-env/autosave-block.json" >/dev/null'

# Prose that merely mentions Claude Code (no concrete coupling) still installs.
make_draft 20260101-000006-g-prose codex-prose-ok "Capture the recurring Codex and Claude Code parity note review procedure." \
"# Mentions Claude Code in prose only

## When to Use
- Comparing Codex and Claude Code behavior.

## Procedure
1. Note the difference between the providers.
2. Record it.
3. Verify.
4. Confirm.

## Verification
- Confirm the recorded note."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex allows prose that only mentions Claude Code" '[ -f "$CODEX_SKILLS/codex-prose-ok/SKILL.md" ]'

# --- 6) existing user-authored Codex skill is never overwritten -----------------
mkdir -p "$CODEX_SKILLS/user-made"
printf -- '---\nname: user-made\ndescription: Operator-authored Codex skill that autosave must never overwrite ever.\n---\n\n# Hand\n' \
  > "$CODEX_SKILLS/user-made/SKILL.md"
before_sha="$(sha256sum "$CODEX_SKILLS/user-made/SKILL.md" | awk '{print $1}')"
make_draft 20260101-000007-h-dup user-made "A different take on the same-named Codex skill with entirely different wording."
run_codex env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run >/dev/null
ok "codex existing skill blocked (never overwrite)" 'jq -e ".reason | startswith(\"dedup already-exists\")" "$PENDING/20260101-000007-h-dup/autosave-block.json" >/dev/null'
ok "codex existing skill content unchanged" '[ "$(sha256sum "$CODEX_SKILLS/user-made/SKILL.md" | awk "{print \$1}")" = "$before_sha" ]'

# --- 7) concurrency: same checkpoint processed 10x → single install, no dup -----
CC_STATE="$TMP/cc-state"; CC_CODEX="$TMP/cc-codex/skills"; CC_SPOOL="$TMP/cc-spool"
mkdir -p "$CC_STATE/pending-skills"
mkdir -p "$CC_CODEX"   # pre-create so all racers share one install target
cc_make() {
  mkdir -p "$CC_STATE/pending-skills/race-1"
  printf -- '---\nname: codex-race-one\ndescription: Capture the recurring Codex concurrency-safe install verification procedure.\n---\n\n# Race

## Procedure
1. Do the recurring step.
2. Verify it.
3. Record.
4. Confirm.
5. Done.\n' > "$CC_STATE/pending-skills/race-1/SKILL.md"
  jq -nc '{id:"race-1", name:"codex-race-one", status:"pending", session_id:"sess-race"}' \
    > "$CC_STATE/pending-skills/race-1/meta.json"
}
cc_make
for _ in $(seq 1 10); do
  CCC_STATE_DIR="$CC_STATE" CCC_SKILL_PROVIDER=codex CODEX_SKILLS_DIR="$CC_CODEX" \
  CCC_PUSH_SPOOL="$CC_SPOOL" CCC_NODE=testnode CCC_SKILL_AUTOSAVE_MODE=auto \
  bash "$AUTO" run >/dev/null 2>&1 &
done
wait
ok "concurrent 10x installs exactly one copy" '[ "$(find "$CC_CODEX" -name SKILL.md | wc -l | tr -d "[:space:]")" = 1 ]'
ok "concurrent 10x writes exactly one install ledger row" '[ "$(jq -r "select(.event==\"install\") | .name" "$CC_STATE/skill-autosave-install.jsonl" 2>/dev/null | grep -c codex-race-one)" = 1 ]'

# --- 8) daily cap defers on the Codex surface -----------------------------------
CAP_STATE="$TMP/cap-state"; CAP_CODEX="$TMP/cap-codex/skills"; CAP_SPOOL="$TMP/cap-spool"
mkdir -p "$CAP_STATE/pending-skills" "$CAP_CODEX"
mkdir -p "$CAP_STATE/pending-skills/cap-a" "$CAP_STATE/pending-skills/cap-b"
printf -- '---\nname: codex-cap-a\ndescription: Capture the first recurring Codex maintenance procedure for the fleet nodes.\n---\n\n# A\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n' > "$CAP_STATE/pending-skills/cap-a/SKILL.md"
printf -- '---\nname: codex-cap-b\ndescription: Capture the second recurring Codex maintenance procedure for backup checks.\n---\n\n# B\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n' > "$CAP_STATE/pending-skills/cap-b/SKILL.md"
jq -nc '{id:"cap-a",name:"codex-cap-a",status:"pending"}' > "$CAP_STATE/pending-skills/cap-a/meta.json"
jq -nc '{id:"cap-b",name:"codex-cap-b",status:"pending"}' > "$CAP_STATE/pending-skills/cap-b/meta.json"
out="$(CCC_STATE_DIR="$CAP_STATE" CCC_SKILL_PROVIDER=codex CODEX_SKILLS_DIR="$CAP_CODEX" \
  CCC_PUSH_SPOOL="$CAP_SPOOL" CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=1 bash "$AUTO" run)"
ok "codex cap installs only one" '[ "$(find "$CAP_CODEX" -name SKILL.md | wc -l | tr -d "[:space:]")" = 1 ]'
ok "codex over-cap draft deferred, not blocked" 'jq -e ".deferred == 1" >/dev/null <<<"$out"'

# --- 9) secure install-dir contract: symlinked skills dir fails closed -----------
SL_STATE="$TMP/sl-state"; SL_REAL="$TMP/sl-real"; SL_LINK="$TMP/sl-link"
mkdir -p "$SL_STATE/pending-skills" "$SL_REAL"
ln -s "$SL_REAL" "$SL_LINK"
mkdir -p "$SL_STATE/pending-skills/sl-1"
printf -- '---\nname: codex-symlink-target\ndescription: Capture the recurring Codex secure install directory verification procedure.\n---\n\n# S\n\n## Procedure\n1. Step.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Done.\n' > "$SL_STATE/pending-skills/sl-1/SKILL.md"
jq -nc '{id:"sl-1",name:"codex-symlink-target",status:"pending"}' > "$SL_STATE/pending-skills/sl-1/meta.json"
out="$(CCC_STATE_DIR="$SL_STATE" CCC_SKILL_PROVIDER=codex CODEX_SKILLS_DIR="$SL_LINK" \
  CCC_PUSH_SPOOL="$TMP/sl-spool" CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "codex symlinked skills dir fails closed" 'jq -e ".skipped == \"unsafe-skills-dir\"" >/dev/null <<<"$out"'
ok "codex symlinked skills dir installs nothing" '[ -z "$(ls -A "$SL_REAL" 2>/dev/null)" ]'

# --- 10) rollback parity + status reports provider -------------------------------
out="$(run_codex bash "$AUTO" rollback codex-clean-one)"
ok "codex rollback removes the skill" '[ ! -e "$CODEX_SKILLS/codex-clean-one" ]'
ok "codex rollback archives, not deletes" 'ls -d "$STATE/skill-autosave-rollback/codex-clean-one."* >/dev/null 2>&1'
run_codex bash "$AUTO" rollback user-made >/dev/null 2>&1; rc=$?
ok "codex rollback refuses user-authored skill" '[ "$rc" != 0 ] && [ -f "$CODEX_SKILLS/user-made/SKILL.md" ]'

out="$(run_codex bash "$AUTO" status)"
ok "codex status reports provider" 'grep -q "^provider: codex" <<<"$out"'
ok "codex status reports codex skills dir" 'grep -q "$CODEX_SKILLS" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
