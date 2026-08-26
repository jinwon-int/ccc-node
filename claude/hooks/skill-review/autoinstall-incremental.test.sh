#!/usr/bin/env bash
# End-to-end v2 proposal routing through autoinstall.sh (#751).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AUTO="$HERE/autoinstall.sh"
OWNERSHIP="$HERE/ownership.py"
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
pass=0
fail=0

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

STATE="$TMP/state"
SKILLS="$TMP/skills"
PENDING="$STATE/pending-skills"
mkdir -m 700 "$STATE" "$SKILLS"
mkdir -m 700 "$PENDING"

tool() {
  python3 "$OWNERSHIP" --provider codex --skills-dir "$SKILLS" --state-dir "$STATE" "$@"
}

run_auto() {
  CCC_SKILL_REVIEW_STATE_DIR="$STATE" CCC_SKILL_PROVIDER=codex CODEX_SKILLS_DIR="$SKILLS" \
    CCC_PUSH_SPOOL="$TMP/spool" CCC_NODE=testnode "$@"
}

make_skill() {
  local name="$1"
  mkdir -m 700 "$SKILLS/$name"
  printf -- '---\nname: %s\ndescription: A sufficiently detailed recurring workflow for incremental integration tests.\n---\n\n# %s\n\n## Procedure\n1. Read.\n2. Verify.\n3. Record.\n' \
    "$name" "$name" > "$SKILLS/$name/SKILL.md"
  chmod 600 "$SKILLS/$name/SKILL.md"
  tool mark-created "$name" >/dev/null
}

stage_patch() { # draft name old new proposal-id
  local draft="$1" name="$2" old="$3" new="$4" proposal_id="$5" sha
  mkdir -m 700 "$PENDING/$draft"
  sha="$(sha256sum "$SKILLS/$name/SKILL.md" | awk '{print $1}')"
  jq -nc --arg id "$proposal_id" --arg name "$name" --arg sha "$sha" \
    --arg old "$old" --arg new "$new" \
    '{
      schema_version:2,
      proposal_id:$id,
      provenance:{
        provider:"codex",
        source_thread_hash:("c"*64),
        trigger:"checkpoint",
        distilled_at:"2026-07-27T00:00:00Z"
      },
      proposal:{
        action:"patch",
        target_skill:$name,
        relative_target:"SKILL.md",
        expected_sha256:$sha,
        old_text:$old,
        new_text:$new,
        improvement_reason:"Apply the repeatable improvement.",
        reason:"Improve the existing overlapping skill.",
        evidence_excerpt:"repeatable improvement"
      }
    }' > "$PENDING/$draft/proposal.json"
  jq -nc --arg id "$draft" --arg name "$name" \
    '{id:$id,name:$name,status:"pending",session_id:("c"*64)}' \
    > "$PENDING/$draft/meta.json"
  chmod 600 "$PENDING/$draft/proposal.json" "$PENDING/$draft/meta.json"
}

stage_write() { # draft name relative content proposal-id
  local draft="$1" name="$2" relative="$3" content="$4" proposal_id="$5"
  local status revision provenance
  mkdir -m 700 "$PENDING/$draft"
  status="$(tool status "$name")"
  revision="$(jq -r '.skills[0].provenance_revision' <<<"$status")"
  provenance="$(jq -r '.skills[0].provenance_sha256' <<<"$status")"
  jq -nc --arg id "$proposal_id" --arg name "$name" --arg relative "$relative" \
    --arg content "$content" --argjson revision "$revision" --arg provenance "$provenance" \
    '{
      schema_version:2,
      proposal_id:$id,
      provenance:{
        provider:"codex",
        source_thread_hash:("d"*64),
        trigger:"checkpoint",
        distilled_at:"2026-07-27T00:00:00Z"
      },
      proposal:{
        action:"write_file",
        target_skill:$name,
        relative_target:$relative,
        expected_absent:true,
        expected_provenance_revision:$revision,
        expected_provenance_sha256:$provenance,
        content:$content,
        improvement_reason:"Add one bounded checklist.",
        reason:"Improve the existing overlapping skill.",
        evidence_excerpt:"bounded checklist"
      }
    }' > "$PENDING/$draft/proposal.json"
  jq -nc --arg id "$draft" --arg name "$name" \
    '{id:$id,name:$name,status:"pending",session_id:("d"*64)}' \
    > "$PENDING/$draft/meta.json"
  chmod 600 "$PENDING/$draft/proposal.json" "$PENDING/$draft/meta.json"
}

stage_legacy_create() { # draft name
  local draft="$1" name="$2"
  mkdir -m 700 "$PENDING/$draft"
  printf -- '---\nname: %s\ndescription: Capture a distinct recurring legacy create integration procedure.\n---\n\n# %s\n\n## Procedure\n1. Read.\n2. Verify.\n3. Record.\n4. Confirm.\n5. Finish.\n' \
    "$name" "$name" > "$PENDING/$draft/SKILL.md"
  jq -nc --arg id "$draft" --arg name "$name" \
    '{id:$id,name:$name,status:"pending",session_id:"legacy"}' \
    > "$PENDING/$draft/meta.json"
  chmod 600 "$PENDING/$draft/SKILL.md" "$PENDING/$draft/meta.json"
}

make_skill routed-skill
stage_patch patch-draft routed-skill "1. Read." "1. Read through v2." "$(printf 'a%.0s' {1..64})"
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=2 bash "$AUTO" run)"
ok "auto mode routes patch through locked ownership apply" \
  'jq -e ".installed == [\"routed-skill:patch\"]" >/dev/null <<<"$out" && grep -q "Read through v2" "$SKILLS/routed-skill/SKILL.md" && ls -d "$PENDING/patch-draft.installed-"* >/dev/null 2>&1'

mkdir -m 700 "$SKILLS/routed-skill/references"
stage_write write-draft routed-skill references/checklist.md $'# Checklist\n\n- Verify routed apply.\n' "$(printf 'b%.0s' {1..64})"
out="$(run_auto bash "$AUTO" render write-draft)"
ok "render exposes owner-review fields without mutation" \
  'jq -e ".action == \"write_file\" and .relative_target == \"references/checklist.md\"" >/dev/null <<<"$out" && [ ! -e "$SKILLS/routed-skill/references/checklist.md" ]'
out="$(run_auto bash "$AUTO" apply write-draft)"
ok "explicit owner apply routes write_file and archives approval" \
  'jq -e ".changed == true and .action == \"write_file\"" >/dev/null <<<"$out" && grep -q "Verify routed apply" "$SKILLS/routed-skill/references/checklist.md" && ls -d "$PENDING/write-draft.approved-"* >/dev/null 2>&1'

stage_legacy_create a-create legacy-created
stage_patch z-patch routed-skill "2. Verify." "2. Verify after cap." "$(printf 'f%.0s' {1..64})"
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto CCC_SKILL_AUTOSAVE_DAILY_CAP=2 bash "$AUTO" run)"
ok "legacy create and incremental apply share one daily cap" \
  'jq -e ".installed == [\"legacy-created\"] and .deferred == 1" >/dev/null <<<"$out" && [ -f "$SKILLS/legacy-created/SKILL.md" ] && grep -q "2. Verify." "$SKILLS/routed-skill/SKILL.md" && [ -d "$PENDING/z-patch" ]'

stage_patch mixed-draft routed-skill "2. Verify." "2. Verify safely." "$(printf 'e%.0s' {1..64})"
touch "$PENDING/mixed-draft/SKILL.md"
out="$(run_auto env CCC_SKILL_AUTOSAVE_MODE=auto bash "$AUTO" run)"
ok "mixed legacy and v2 payload fails closed" \
  'jq -e ".blocked[0].reason == \"proposal mixed-payload\"" >/dev/null <<<"$out" && [ -d "$PENDING/mixed-draft" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
