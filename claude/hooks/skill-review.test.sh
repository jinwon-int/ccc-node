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
{"skill_candidates":[{"name":"deploy-checklist","category":"ops","summary":"Capture a recurring deploy checklist.","reason":"The transcript repeats a multi-step deploy verification flow.","evidence_excerpt":"automate recurring deploy checklist","skill_md":"---\nname: deploy-checklist\ndescription: Use when capturing deploy checklist procedures: inspect git state, then verify.\n---\n\n# Deploy Checklist\n\n## When to Use\n- Use when deploy verification repeats.\n\n## Procedure\n1. Inspect git state.\n2. Run the verified checklist.\n\n## Safety\n- Never store raw secrets.\n\n## Verification\n- Confirm the checklist output is recorded.\n"}]}
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
# #2032: the model wrote the description unquoted with ": "; the staged draft
# carries it as one YAML-safe double-quoted line.
ok "skill-review stages a YAML-safe description line" \
  'grep -Fxq "description: \"Use when capturing deploy checklist procedures: inspect git state, then verify.\"" "$(find "$STATE/pending-skills" -name SKILL.md | head -1)"'
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
{"skill_candidates":[{"name":"neutral-probe","category":"piri","summary":"Probe emitted by the neutral LLM command.","reason":"Synthetic fixture response.","evidence_excerpt":"fixture","skill_md":"---\nname: neutral-probe\ndescription: Use when probing the skill emitted by the fake neutral LLM command fixture.\n---\n\n# Neutral Probe\n\n## When to Use\n- Never; this is a fixture.\n\n## Procedure\n1. Emit.\n\n## Safety\n- No secrets.\n\n## Verification\n- Fixture only.\n"}]}
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
# fleet-skills#315/#316: descriptions must lead with the trigger, or the draft
# is blocked at install (lint description-trigger) and at promotion.
ok "prompt requires a trigger-first description" \
  'grep -q "MUST START with the trigger" "$LLM_SNAPSHOT" && grep -q "\"Use when <situation> ...\"" "$LLM_SNAPSHOT" && grep -q "할 때 사용" "$LLM_SNAPSHOT" && grep -q "at most 300 characters" "$LLM_SNAPSHOT"'
ok "prompt asks for a general name and an overlap check against existing skills" \
  'grep -q "general, reusable capability name" "$LLM_SNAPSHOT" && grep -q "Check the existing skills list" "$LLM_SNAPSHOT" && grep -q "near-duplicate" "$LLM_SNAPSHOT"'
# #2388: the drafting brain reads the shared trajectory grammar — commands are
# shown (they are the procedure a skill reuses) and the prompt says the
# render-truncation marker is the renderer's, not a failed step.
ok "prompt carries the shared transcript grammar + marker contract" \
  'grep -qF "Transcript grammar: every block starts with one tag" "$LLM_SNAPSHOT" && grep -qF "they are not agent, tool, or runtime behavior" "$LLM_SNAPSHOT"'
ok "transcript commands reach the drafting brain as [ACTION] Bash(<command>)" \
  'grep -qx "\[ACTION\] Bash(git status --short)" "$LLM_SNAPSHOT" && ! grep -q "\[tool:Bash\]" "$LLM_SNAPSHOT"'
ok "neutral fixture draft itself satisfies the trigger lint" \
  'jq -r ".skill_candidates[0].skill_md" "$TMP/llm-out.json" | grep "^description:" | sed "s/^description: //" | python3 "$HERE/skill-review/description_trigger.py" check'

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

# #1932: a too-few-turns skip stays exit 0 for hooks; only an explicit batch
# caller (the autosave sweep) gets its skip code back, so skips stop consuming
# the sweep's drafting budget.
STATE_SKIP="$TMP/state-skip"
mkdir -p "$STATE_SKIP"; chmod 700 "$STATE_SKIP"
SHORT="$TMP/projects/-root--work/sess-short.jsonl"
make_transcript "$SHORT" 1
payload sess-short "$SHORT" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE_SKIP" CLAUDE_SKILLS_DIR="$SKILLS" \
  bash "$REVIEW" manual >/dev/null 2>&1; rc=$?
ok "#1932: too-few-turns skip exits 0 without an explicit skip code" '[ "$rc" = 0 ]'
payload sess-short "$SHORT" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE_SKIP" CLAUDE_SKILLS_DIR="$SKILLS" \
  CCC_SKILL_REVIEW_SKIP_RC=3 bash "$REVIEW" manual >/dev/null 2>&1; rc=$?
ok "#1932: too-few-turns skip returns the requested skip code" '[ "$rc" = 3 ]'
payload sess-short "$SHORT" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE_SKIP" CLAUDE_SKILLS_DIR="$SKILLS" \
  CCC_SKILL_REVIEW_SKIP_RC=bogus bash "$REVIEW" manual >/dev/null 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "#1932: a malformed skip code falls back to exit 0" '[ "$rc" = 0 ]'
ok "#1932: the skip is still logged as too-few-turns" \
  '[ "$(grep -c "skip reason=too-few-turns" "$STATE_SKIP/skill-review.log")" = 3 ]'

# --- #2239: draft-time gates -------------------------------------------------
# The staging loop rejects trigger-less, near-duplicate and over-cap candidates
# into skill-autosave-archive/draft-rejected/<id>/ instead of pending-skills/.
# Fixtures drive the synchronous background re-entry (CLAUDE_SKILL_REVIEW_BG=1)
# so every assertion is deterministic; one case at the end goes through the
# real SessionEnd spawn path to prove the hook reaches the same gates. The
# fake LLM echoes whichever candidate list SR_GATE_FIXTURE points at.
mkdir -p "$TMP/bin-gates" "$TMP/fixtures"
write_exec_stub "$TMP/bin-gates/fake-llm" <<'SH'
cat >/dev/null
cat "$SR_GATE_FIXTURE"
SH
chmod +x "$TMP/bin-gates/fake-llm"
# candidate <name> <description> — one skill_candidates entry (compact JSON)
candidate() {
  jq -nc --arg n "$1" --arg d "$2" \
    '{name:$n, category:"ops", summary:"fixture", reason:"fixture", evidence_excerpt:"fixture",
      skill_md:("---\nname: " + $n + "\ndescription: " + $d + "\n---\n\n# " + $n + "\n\n## When to Use\n- Fixture.\n\n## Procedure\n1. Step.\n2. Step.\n\n## Safety\n- No secrets.\n\n## Verification\n- Fixture.\n")}'
}
fixture() { # <file> <candidate-json>... — wraps candidates into the LLM reply
  local f="$1"; shift
  printf '%s\n' "$@" | jq -sc '{skill_candidates: .}' > "$f"
}
GATE_SKILLS="$TMP/skills-gates"
mkdir -p "$GATE_SKILLS"; chmod 700 "$GATE_SKILLS"
run_gates_bg() { # <state> <session> <fixture> [VAR=value ...]
  local state="$1" sid="$2" fix="$3"; shift 3
  env CLAUDE_SKILL_REVIEW_BG=1 CLAUDE_SKILL_REVIEW_INFLIGHT=1 CLAUDE_SKILL_REVIEW_TRIGGER=sessionend \
    CLAUDE_SKILL_REVIEW_TRANSCRIPT="$TRANS" CLAUDE_SKILL_REVIEW_SESSION="$sid" \
    CCC_SKILL_REVIEW_STATE_DIR="$state" CLAUDE_SKILLS_DIR="$GATE_SKILLS" \
    CCC_SKILL_REVIEW_LLM_CMD="$TMP/bin-gates/fake-llm" SR_GATE_FIXTURE="$fix" "$@" \
    bash "$REVIEW" sessionend >/dev/null 2>&1
}
rejected_dir() { # <state> <name> — the draft-rejected/<id> dir for a candidate name
  find "$1/skill-autosave-archive/draft-rejected" -mindepth 1 -maxdepth 1 -type d -name "*-$2" 2>/dev/null | head -1
}
pending_count_of() { find "$1/pending-skills" -mindepth 2 -maxdepth 2 -name SKILL.md 2>/dev/null | grep -c .; }

# (a) trigger wording missing -> rejected to the archive with reject.json
STATE_G1="$TMP/state-g1"; mkdir -p "$STATE_G1"; chmod 700 "$STATE_G1"
fixture "$TMP/fixtures/no-trigger.json" \
  "$(candidate no-trigger-skill "Captures the deploy checklist procedure for later reuse by the team.")"
run_gates_bg "$STATE_G1" sess-g1 "$TMP/fixtures/no-trigger.json"; rc=$?
ok "#2239 trigger gate: hook exits 0" '[ "$rc" = 0 ]'
ok "#2239 trigger gate: nothing staged into pending-skills" '[ "$(pending_count_of "$STATE_G1")" = 0 ]'
# shellcheck disable=SC2034  # read via eval inside ok()
rej1="$(rejected_dir "$STATE_G1" no-trigger-skill)"
ok "#2239 trigger gate: draft archived under draft-rejected/<id>/SKILL.md" \
  '[ -n "$rej1" ] && [ -f "$rej1/SKILL.md" ] && grep -q "^name: no-trigger-skill" "$rej1/SKILL.md"'
ok "#2239 trigger gate: reject.json carries reason/session/ts/name" \
  'jq -e ".reason == \"description-trigger\" and .session == \"sess-g1\" and .name == \"no-trigger-skill\" and (.ts | length) > 0" "$rej1/reject.json" >/dev/null'
ok "#2239 trigger gate: archive is owner-only (dir 700, files 600)" \
  '[ "$(stat -c %a "$rej1")" = 700 ] && [ "$(stat -c %a "$rej1/SKILL.md")" = 600 ] && [ "$(stat -c %a "$rej1/reject.json")" = 600 ]'
ok "#2239 trigger gate: archived meta.json keeps the candidate record" \
  'jq -e ".status == \"rejected\" and .reject_reason == \"description-trigger\" and (.skill_md | not)" "$rej1/meta.json" >/dev/null'
ok "#2283 lifecycle: draft-rejected meta and archive manifest row carry archived:draft-rejected" \
  'jq -e ".lifecycle == \"archived:draft-rejected\"" "$rej1/meta.json" >/dev/null && jq -e "select(.name == \"$(basename "$rej1")\") | .lifecycle == \"archived:draft-rejected\"" "$(dirname "$rej1")/manifest.jsonl" >/dev/null'
ok "#2239 trigger gate: review log line" \
  'grep -q "candidate rejected reason=description-trigger name=no-trigger-skill" "$STATE_G1/skill-review.log"'
ok "#2239 trigger gate: manifest row (pending-expire shape)" \
  'jq -e ".reason == \"draft-gate:description-trigger\" and .skill == \"no-trigger-skill\"" "$STATE_G1/skill-autosave-archive/draft-rejected/manifest.jsonl" >/dev/null'
ok "#2239 trigger gate: skill-review-last.json tallies the rejection" \
  'jq -e ".staged == 0 and .draft_gates == \"on\" and .rejected == {trigger:1, duplicate:0, session_cap:0}" "$STATE_G1/skill-review-last.json" >/dev/null'
ok "#2239 trigger gate: no approval marker for a fully rejected run" '[ ! -s "$STATE_G1/approval-needed.log" ]'
ok "#2239 trigger gate: work dir cleaned up" '! ls -d "$STATE_G1"/.skill-review-work-* >/dev/null 2>&1'
# A rejected draft can be pulled back into the queue by the existing restore verb.
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
out="$(CCC_SKILL_REVIEW_STATE_DIR="$STATE_G1" python3 "$HERE/skill-review/pending_expire.py" restore "$(basename "$rej1")")"; rc=$?
ok "#2239 trigger gate: pending-restore brings the rejected draft back" \
  '[ "$rc" = 0 ] && printf "%s" "$out" | jq -e ".status == \"restored\"" >/dev/null && [ -f "$STATE_G1/pending-skills/$(basename "$rej1")/SKILL.md" ] && [ ! -e "$rej1" ]'

# (b) duplicate vs an INSTALLED skill: same description -> rejected, matched recorded
STATE_G2="$TMP/state-g2"; mkdir -p "$STATE_G2"; chmod 700 "$STATE_G2"
mkdir -p "$GATE_SKILLS/existing-skill"
printf -- '---\nname: existing-skill\ndescription: Use when running the recurring wiki record procedure for durable decisions.\n---\n\n# Existing\n' \
  > "$GATE_SKILLS/existing-skill/SKILL.md"
fixture "$TMP/fixtures/dup-installed.json" \
  "$(candidate wiki-recorder "Use when running the recurring wiki record procedure for durable decisions.")"
run_gates_bg "$STATE_G2" sess-g2 "$TMP/fixtures/dup-installed.json"
# shellcheck disable=SC2034  # read via eval inside ok()
rej2="$(rejected_dir "$STATE_G2" wiki-recorder)"
ok "#2239 dup/installed: nothing staged" '[ "$(pending_count_of "$STATE_G2")" = 0 ]'
ok "#2239 dup/installed: rejected with the matched installed name" \
  '[ -n "$rej2" ] && jq -e ".reason == \"duplicate\" and .matched == \"existing-skill\" and .match.scope == \"installed\" and .match.kind == \"description-similar\"" "$rej2/reject.json" >/dev/null'
ok "#2239 dup/installed: log names the match" \
  'grep -q "candidate rejected reason=duplicate name=wiki-recorder .*matched=existing-skill" "$STATE_G2/skill-review.log"'
# Same name as an installed dir (different wording) -> already-exists
fixture "$TMP/fixtures/dup-name.json" \
  "$(candidate existing-skill "Use when taking another pass at the wiki record flow with different wording entirely.")"
run_gates_bg "$STATE_G2" sess-g2b "$TMP/fixtures/dup-name.json"
# shellcheck disable=SC2034  # read via eval inside ok()
rej2b="$(rejected_dir "$STATE_G2" existing-skill)"
ok "#2239 dup/installed: existing skill dir name is a hit (already-exists)" \
  '[ -n "$rej2b" ] && jq -e ".matched == \"existing-skill\" and .match.kind == \"already-exists\"" "$rej2b/reject.json" >/dev/null'
ok "#2239 dup/installed: stash tallies duplicate" \
  'jq -e ".rejected.duplicate == 1 and .rejected.trigger == 0" "$STATE_G2/skill-review-last.json" >/dev/null'

# (c) duplicate vs a PENDING draft, with the stricter threshold. The pending
# draft and the first candidate share 5 of 7 tokens (71%): over the installed
# threshold (60) but under the pending one (75) -> staged. An identical
# description (100%) -> rejected against the pending draft. The same 71%
# description installed as a skill -> rejected against installed.
STATE_G3="$TMP/state-g3"; mkdir -p "$STATE_G3/pending-skills/20260101-000000-seed-release-gate-checklist"; chmod 700 "$STATE_G3"
printf -- '---\nname: release-gate-checklist\ndescription: Use when verifying release gate checklist before tagging a build.\n---\n\n# Seed\n' \
  > "$STATE_G3/pending-skills/20260101-000000-seed-release-gate-checklist/SKILL.md"
fixture "$TMP/fixtures/near-pending.json" \
  "$(candidate release-gate-verify "Use when verifying release gate checklist before tagging deploys.")"
run_gates_bg "$STATE_G3" sess-g3a "$TMP/fixtures/near-pending.json"
ok "#2239 dup/pending: 71% overlap with a pending draft is NOT a duplicate (stricter threshold)" \
  '[ "$(pending_count_of "$STATE_G3")" = 2 ] && [ -z "$(rejected_dir "$STATE_G3" release-gate-verify)" ]'
fixture "$TMP/fixtures/same-pending.json" \
  "$(candidate release-gate-check "Use when verifying release gate checklist before tagging a build.")"
run_gates_bg "$STATE_G3" sess-g3b "$TMP/fixtures/same-pending.json"
# shellcheck disable=SC2034  # read via eval inside ok()
rej3="$(rejected_dir "$STATE_G3" release-gate-check)"
ok "#2239 dup/pending: identical description to a pending draft is rejected" \
  '[ "$(pending_count_of "$STATE_G3")" = 2 ] && [ -n "$rej3" ] && jq -e ".reason == \"duplicate\" and .matched == \"release-gate-checklist\" and .match.scope == \"pending\"" "$rej3/reject.json" >/dev/null'
mkdir -p "$GATE_SKILLS/release-gate-checklist"
printf -- '---\nname: release-gate-checklist\ndescription: Use when verifying release gate checklist before tagging a build.\n---\n\n# Installed\n' \
  > "$GATE_SKILLS/release-gate-checklist/SKILL.md"
fixture "$TMP/fixtures/near-installed.json" \
  "$(candidate release-gate-tagging "Use when verifying release gate checklist before tagging deploys.")"
run_gates_bg "$STATE_G3" sess-g3c "$TMP/fixtures/near-installed.json"
ok "#2239 dup/installed: the same 71% overlap IS a duplicate against an installed skill" \
  '[ -n "$(rejected_dir "$STATE_G3" release-gate-tagging)" ] && jq -e ".match.scope == \"installed\"" "$(rejected_dir "$STATE_G3" release-gate-tagging)/reject.json" >/dev/null'
# Pending threshold is an env knob: lowering it to the installed level rejects the 71% case too.
rm -rf "$GATE_SKILLS/release-gate-checklist"
fixture "$TMP/fixtures/near-pending-2.json" \
  "$(candidate release-gate-tagger "Use when verifying release gate checklist before tagging deploys.")"
run_gates_bg "$STATE_G3" sess-g3d "$TMP/fixtures/near-pending-2.json" CCC_SKILL_DEDUP_PENDING_JACCARD_PCT=60
ok "#2239 dup/pending: CCC_SKILL_DEDUP_PENDING_JACCARD_PCT lowers the pending threshold" \
  '[ -n "$(rejected_dir "$STATE_G3" release-gate-tagger)" ]'

# (d) per-session cap: default 1 stages the first good candidate, the rest go
# to the archive as session-cap; 0 disables; N caps at N.
STATE_G4="$TMP/state-g4"; mkdir -p "$STATE_G4"; chmod 700 "$STATE_G4"
fixture "$TMP/fixtures/three.json" \
  "$(candidate alpha-backup-rotation "Use when rotating encrypted backup archives across storage tiers monthly.")" \
  "$(candidate beta-dns-failover "Use when switching DNS records to the standby region during an outage.")" \
  "$(candidate gamma-cert-renewal "Use when renewing expiring TLS certificates on edge proxies.")"
run_gates_bg "$STATE_G4" sess-g4 "$TMP/fixtures/three.json"
ok "#2239 session cap: default 1 stages exactly one candidate" \
  '[ "$(pending_count_of "$STATE_G4")" = 1 ] && [ -f "$(find "$STATE_G4/pending-skills" -name SKILL.md | head -1)" ] && grep -q "^name: alpha-backup-rotation" "$(find "$STATE_G4/pending-skills" -name SKILL.md | head -1)"'
ok "#2239 session cap: the other two are archived as session-cap" \
  '[ -n "$(rejected_dir "$STATE_G4" beta-dns-failover)" ] && [ -n "$(rejected_dir "$STATE_G4" gamma-cert-renewal)" ] && jq -e ".reason == \"session-cap\"" "$(rejected_dir "$STATE_G4" gamma-cert-renewal)/reject.json" >/dev/null'
ok "#2239 session cap: stash tallies session_cap=2" \
  'jq -e ".staged == 1 and .rejected.session_cap == 2" "$STATE_G4/skill-review-last.json" >/dev/null'
ok "#2239 session cap: approval marker counts only the staged draft" \
  'grep -q "PENDING_SKILL_REVIEW staged=1 pending=1 session=sess-g4" "$STATE_G4/approval-needed.log"'
STATE_G4B="$TMP/state-g4b"; mkdir -p "$STATE_G4B"; chmod 700 "$STATE_G4B"
run_gates_bg "$STATE_G4B" sess-g4b "$TMP/fixtures/three.json" CCC_SKILL_REVIEW_MAX_CANDIDATES=0
ok "#2239 session cap: CCC_SKILL_REVIEW_MAX_CANDIDATES=0 lifts the cap" \
  '[ "$(pending_count_of "$STATE_G4B")" = 3 ] && jq -e ".rejected.session_cap == 0" "$STATE_G4B/skill-review-last.json" >/dev/null'
STATE_G4C="$TMP/state-g4c"; mkdir -p "$STATE_G4C"; chmod 700 "$STATE_G4C"
run_gates_bg "$STATE_G4C" sess-g4c "$TMP/fixtures/three.json" CCC_SKILL_REVIEW_MAX_CANDIDATES=2
ok "#2239 session cap: CCC_SKILL_REVIEW_MAX_CANDIDATES=2 stages two" \
  '[ "$(pending_count_of "$STATE_G4C")" = 2 ] && jq -e ".rejected.session_cap == 1" "$STATE_G4C/skill-review-last.json" >/dev/null'
STATE_G4D="$TMP/state-g4d"; mkdir -p "$STATE_G4D"; chmod 700 "$STATE_G4D"
run_gates_bg "$STATE_G4D" sess-g4d "$TMP/fixtures/three.json" CCC_SKILL_REVIEW_MAX_CANDIDATES=bogus
ok "#2239 session cap: a malformed cap falls back to the default 1" \
  '[ "$(pending_count_of "$STATE_G4D")" = 1 ]'

# (e) gates off: the trigger-less draft and all three candidates are staged as before
STATE_G5="$TMP/state-g5"; mkdir -p "$STATE_G5"; chmod 700 "$STATE_G5"
run_gates_bg "$STATE_G5" sess-g5 "$TMP/fixtures/no-trigger.json" CCC_SKILL_REVIEW_DRAFT_GATES=off
run_gates_bg "$STATE_G5" sess-g5b "$TMP/fixtures/three.json" CCC_SKILL_REVIEW_DRAFT_GATES=off
ok "#2239 gates off: trigger-less and over-cap candidates are staged" \
  '[ "$(pending_count_of "$STATE_G5")" = 4 ] && [ ! -d "$STATE_G5/skill-autosave-archive/draft-rejected" ]'
ok "#2239 gates off: stash records draft_gates=off with zero rejections" \
  'jq -e ".draft_gates == \"off\" and .rejected == {trigger:0, duplicate:0, session_cap:0}" "$STATE_G5/skill-review-last.json" >/dev/null'

# (f) happy path unchanged: the original deploy-checklist run above staged
# SKILL.md + meta.json and installed nothing; its stash now also shows the
# gate tallies at zero.
ok "#2239 happy path: approve-mode stash shows gates on and nothing rejected" \
  'jq -e ".staged == 1 and .draft_gates == \"on\" and .rejected == {trigger:0, duplicate:0, session_cap:0}" "$STATE/skill-review-last.json" >/dev/null'
ok "#2239 happy path: no draft-rejected archive was created" '[ ! -e "$STATE/skill-autosave-archive/draft-rejected" ]'

# (g) the real SessionEnd hook path (payload on stdin -> detached spawn ->
# background re-entry) reaches the same gates: a trigger-less candidate ends
# in the archive, not the queue.
STATE_G6="$TMP/state-g6"; mkdir -p "$STATE_G6"; chmod 700 "$STATE_G6"
payload sess-g6 "$TRANS" "/root/work" | CCC_SKILL_REVIEW_STATE_DIR="$STATE_G6" CLAUDE_SKILLS_DIR="$GATE_SKILLS" \
  CCC_SKILL_REVIEW_COOLDOWN_SECONDS=0 CCC_SKILL_REVIEW_LLM_CMD="$TMP/bin-gates/fake-llm" \
  SR_GATE_FIXTURE="$TMP/fixtures/no-trigger.json" bash "$REVIEW" sessionend >/dev/null 2>&1
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "#2239 SessionEnd path: hook exits 0" '[ "$rc" = 0 ]'
for _ in $(seq 1 40); do
  grep -q "done staged=0" "$STATE_G6/skill-review.log" 2>/dev/null && break
  sleep 0.1
done
ok "#2239 SessionEnd path: trigger-less draft rejected through the hook spawn" \
  '[ "$(pending_count_of "$STATE_G6")" = 0 ] && [ -n "$(rejected_dir "$STATE_G6" no-trigger-skill)" ] && grep -q "candidate rejected reason=description-trigger name=no-trigger-skill" "$STATE_G6/skill-review.log"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
