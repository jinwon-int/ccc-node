#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROMOTER="$HERE/ccc-skill-promotion.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

HOME_DIR="$TMP/home"
STATE="$HOME_DIR/.claude/state"
CLAUDE_SKILLS="$HOME_DIR/.claude/skills"
CODEX_SKILLS="$HOME_DIR/.codex/skills"
BIN="$TMP/bin"
mkdir -p "$STATE" "$CLAUDE_SKILLS" "$CODEX_SKILLS" "$BIN"
chmod 700 "$HOME_DIR" "$HOME_DIR/.claude" "$STATE" "$CLAUDE_SKILLS" \
  "$HOME_DIR/.codex" "$CODEX_SKILLS" "$BIN"

OWNERSHIP="$TMP/ownership.py"
STATUS_JSON="$TMP/status.json"
cat > "$OWNERSHIP" <<'PY'
#!/usr/bin/env python3
import os
from pathlib import Path
print(Path(os.environ["PROMOTION_TEST_STATUS"]).read_text(encoding="utf-8"))
PY
chmod 700 "$OWNERSHIP"

write_skill() {
  local name="$1" body="$2" created_by="${3:-ccc-node}"
  local dir="$CLAUDE_SKILLS/$name" sha
  mkdir -p "$dir"
  chmod 700 "$dir"
  printf -- '---\nname: %s\ndescription: Capture a reusable and safely shareable release verification procedure.\n---\n\n# Procedure\n\n1. Inspect the release state.\n2. Run the bounded verification.\n3. Record the result.\n%s\n' \
    "$name" "$body" > "$dir/SKILL.md"
  chmod 600 "$dir/SKILL.md"
  sha="$(sha256sum "$dir/SKILL.md" | awk '{print $1}')"
  jq -nc --arg name "$name" --arg sha "$sha" --arg created "$created_by" \
    '{schema_version:2,manager:"ccc-node-skill-autosave",ownership:"autosave-managed",
      provider:"claude",name:$name,target_id:("target-"+$name),skill_sha256:$sha,
      created_by:$created,provenance_revision:1,rollback_eligible:true}' \
    > "$dir/.autosave-meta.json"
  chmod 600 "$dir/.autosave-meta.json"
}

write_status() {
  local name="$1" sha
  sha="$(sha256sum "$CLAUDE_SKILLS/$name/SKILL.md" | awk '{print $1}')"
  jq -nc --arg name "$name" --arg sha "$sha" \
    '{skills:[{autonomous_write_allowed:true,classification:"autosave-managed",
      pinned:false,provider:"claude",name:$name,target_id:("target-"+$name),
      skill_sha256:$sha,provenance_revision:1}]}' > "$STATUS_JSON"
}

base_env=(
  "HOME=$HOME_DIR"
  "CCC_CLAUDE_DIR=$HOME_DIR/.claude"
  "CCC_STATE_DIR=$STATE"
  "CCC_SKILL_PROMOTION_OWNERSHIP_TOOL=$OWNERSHIP"
  "CCC_SKILL_PROMOTION_CLAUDE_SKILLS_DIR=$CLAUDE_SKILLS"
  "CCC_SKILL_PROMOTION_CODEX_SKILLS_DIR=$CODEX_SKILLS"
  "CCC_SKILL_PROMOTION_PROVIDERS=claude"
  "CCC_SKILL_PROMOTION_REPO=test/repo"
  "CCC_NODE=testnode"
  "PROMOTION_TEST_STATUS=$STATUS_JSON"
)

# Disabled is the safe default and does not require a readable ownership result.
printf '{"skills":[]}\n' > "$STATUS_JSON"
out="$(env "${base_env[@]}" python3 "$PROMOTER" status)"; rc=$?
ok "disabled status is read-only and successful" \
  '[ "$rc" = 0 ] && jq -e ".ok and (.enabled == false)" >/dev/null <<<"$out"'

# One valid autosave-managed skill is planned without network or repository writes.
write_skill release-checklist ""
write_status release-checklist
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "dry-run plans a bounded draft PR" \
  '[ "$rc" = 0 ] && jq -e ".published[0].outcome == \"would-open-draft-pr\" and .published[0].name == \"release-checklist\"" >/dev/null <<<"$out"'
ok "dry-run creates no ledger" '[ ! -e "$STATE/skill-promotion/ledger.jsonl" ]'
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true CCC_AUTONOMY=dry-run python3 "$PROMOTER" run)"; rc=$?
ok "direct promoter honors fleet autonomy dry-run" \
  '[ "$rc" = 0 ] && jq -e ".mode == \"dry-run\" and .autonomy == \"dry-run\" and .published[0].outcome == \"would-open-draft-pr\"" >/dev/null <<<"$out"'
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true CCC_AUTONOMY=kill python3 "$PROMOTER" run)"; rc=$?
ok "direct promoter honors fleet autonomy kill" \
  '[ "$rc" = 0 ] && jq -e ".status == \"autonomy-kill\" and .published == []" >/dev/null <<<"$out"'

# Local bare repository and gh stub make the publication path hermetic.
SEED="$TMP/seed"
REMOTE="$TMP/remote.git"
mkdir -p "$SEED/codex" "$SEED/skills/shared" "$SEED/claude/skills" "$SEED/codex/skills"
printf '{"schema_version":1,"classifications":[],"managed_skills":[]}\n' \
  > "$SEED/codex/compatibility.json"
git -C "$SEED" init -q -b main
git -C "$SEED" -c user.name=test -c user.email=test@example.invalid add .
git -C "$SEED" -c user.name=test -c user.email=test@example.invalid commit -qm seed
git clone -q --bare "$SEED" "$REMOTE"

GH_STATE="$TMP/gh-state"
write_exec_stub "$BIN/gh" <<'SH'
set -eu
case "${1:-} ${2:-}" in
  "auth status") exit 0 ;;
  "pr list")
    if [ -f "$GH_TEST_STATE/created" ]; then
      printf '[{"url":"https://github.com/test/repo/pull/1","state":"OPEN","isDraft":true}]\n'
    else
      printf '[]\n'
    fi
    ;;
  "pr create")
    mkdir -p "$GH_TEST_STATE"
    printf '%s\n' "$*" >> "$GH_TEST_STATE/create.args"
    : > "$GH_TEST_STATE/created"
    printf 'https://github.com/test/repo/pull/1\n'
    ;;
  *) exit 9 ;;
esac
SH

actual_env=(
  "${base_env[@]}"
  "CCC_SKILL_PROMOTION_ENABLED=true"
  "CCC_SKILL_PROMOTION_REMOTE=$REMOTE"
  "GH_TEST_STATE=$GH_STATE"
  "PATH=$BIN:$PATH"
)
out="$(env "${actual_env[@]}" python3 "$PROMOTER" run)"; rc=$?
branch="$(jq -r '.published[0].branch' <<<"$out")"
ok "live run opens a draft PR only" \
  '[ "$rc" = 0 ] && jq -e ".published[0].outcome == \"pr-opened\" and .published[0].draft == \"true\"" >/dev/null <<<"$out" && grep -q -- "--draft" "$GH_STATE/create.args"'
ok "promotion branch contains the skill and generated Codex interface" \
  'git --git-dir="$REMOTE" show "$branch:skills/shared/release-checklist/SKILL.md" >/dev/null && git --git-dir="$REMOTE" show "$branch:skills/shared/release-checklist/agents/openai.yaml" | grep -q "\$release-checklist"'
ok "promotion updates the compatibility catalog" \
  'git --git-dir="$REMOTE" show "$branch:codex/compatibility.json" | jq -e ".managed_skills[0].name == \"release-checklist\" and .classifications[0].compatibility == \"adapted\"" >/dev/null'
ok "body-free owner ledger records the proposal" \
  '[ "$(stat -c %a "$STATE/skill-promotion/ledger.jsonl")" = 600 ] && jq -e ".outcome == \"pr-opened\"" "$STATE/skill-promotion/ledger.jsonl" >/dev/null'

before="$(grep -c '^pr create' "$GH_STATE/create.args")"
out="$(env "${actual_env[@]}" python3 "$PROMOTER" run)"; rc=$?
after="$(grep -c '^pr create' "$GH_STATE/create.args")"
ok "content-addressed rerun reuses the existing PR" \
  '[ "$rc" = 0 ] && [ "$before" = "$after" ] && jq -e ".published[0].outcome == \"existing-pr\"" >/dev/null <<<"$out"'

write_skill zeta-checklist ""
release_sha="$(sha256sum "$CLAUDE_SKILLS/release-checklist/SKILL.md" | awk '{print $1}')"
zeta_sha="$(sha256sum "$CLAUDE_SKILLS/zeta-checklist/SKILL.md" | awk '{print $1}')"
jq -nc --arg release_sha "$release_sha" --arg zeta_sha "$zeta_sha" \
  '{skills:[
    {autonomous_write_allowed:true,classification:"autosave-managed",pinned:false,
      provider:"claude",name:"release-checklist",target_id:"target-release-checklist",
      skill_sha256:$release_sha,provenance_revision:1},
    {autonomous_write_allowed:true,classification:"autosave-managed",pinned:false,
      provider:"claude",name:"zeta-checklist",target_id:"target-zeta-checklist",
      skill_sha256:$zeta_sha,provenance_revision:1}]}' > "$STATUS_JSON"
before="$(grep -c '^pr create' "$GH_STATE/create.args")"
out="$(env "${actual_env[@]}" python3 "$PROMOTER" run)"; rc=$?
after="$(grep -c '^pr create' "$GH_STATE/create.args")"
ok "an existing proposal does not starve the next candidate" \
  '[ "$rc" = 0 ] && [ "$after" -eq $((before + 1)) ] && jq -e "(.published | map(.outcome)) == [\"existing-pr\",\"pr-opened\"] and .published[1].name == \"zeta-checklist\"" >/dev/null <<<"$out"'

# A promoted branch must satisfy the real repository's Codex skill catalog.
write_skill catalog-contract ""
write_status catalog-contract
FULL_REMOTE="$TMP/full-remote.git"
FULL_GH_STATE="$TMP/full-gh-state"
git clone -q --bare "$HERE/.." "$FULL_REMOTE"
full_env=(
  "${base_env[@]}"
  "CCC_SKILL_PROMOTION_ENABLED=true"
  "CCC_SKILL_PROMOTION_REMOTE=$FULL_REMOTE"
  "GH_TEST_STATE=$FULL_GH_STATE"
  "PATH=$BIN:$PATH"
)
out="$(env "${full_env[@]}" python3 "$PROMOTER" run)"; rc=$?
full_branch="$(jq -r '.published[0].branch' <<<"$out")"
VALIDATE_REPO="$TMP/validate-repo"
git clone -q --single-branch --branch "$full_branch" "$FULL_REMOTE" "$VALIDATE_REPO"
validation="$(python3 "$HERE/ccc_codex_skills.py" \
  --repo-root "$VALIDATE_REPO" --codex-home "$TMP/validate-codex-home" validate)"; validate_rc=$?
ok "generated shared skill satisfies the production Codex catalog" \
  '[ "$rc" = 0 ] && [ "$validate_rc" = 0 ] && jq -e ".ok and (.managed_skills > 0)" >/dev/null <<<"$validation" && git --git-dir="$FULL_REMOTE" show "$full_branch:codex/compatibility.json" | jq -e ".managed_skills | map(.name) | index(\"catalog-contract\")" >/dev/null'

# A differently named but substantially identical central skill is deduplicated.
DUP_SEED="$TMP/dup-seed"
DUP_REMOTE="$TMP/dup-remote.git"
git clone -q "$SEED" "$DUP_SEED"
mkdir -p "$DUP_SEED/skills/shared/existing-release"
printf -- '---\nname: existing-release\ndescription: Capture a reusable and safely shareable release verification procedure.\n---\n\n# Existing\n\n1. Inspect.\n2. Verify.\n3. Record.\n' \
  > "$DUP_SEED/skills/shared/existing-release/SKILL.md"
git -C "$DUP_SEED" -c user.name=test -c user.email=test@example.invalid add .
git -C "$DUP_SEED" -c user.name=test -c user.email=test@example.invalid commit -qm duplicate
git clone -q --bare "$DUP_SEED" "$DUP_REMOTE"
write_skill alternate-release ""
write_status alternate-release
dup_env=(
  "${base_env[@]}"
  "CCC_SKILL_PROMOTION_ENABLED=true"
  "CCC_SKILL_PROMOTION_REMOTE=$DUP_REMOTE"
  "GH_TEST_STATE=$TMP/dup-gh-state"
  "PATH=$BIN:$PATH"
)
out="$(env "${dup_env[@]}" python3 "$PROMOTER" run)"; rc=$?
ok "central description dedup blocks a redundant proposal" \
  '[ "$rc" = 2 ] && jq -e ".published == [] and .errors[0].code == \"central_description_similar\"" >/dev/null <<<"$out"'

# A fresh pre-publication scan fails closed on secrets.
write_skill leaky-skill '4. token=abcdefghijklmnop1234567890'
write_status leaky-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "credential-shaped content stays local" \
  '[ "$rc" = 0 ] && jq -e ".published == [] and .blocked[0].code == \"secret_credential-assignment\"" >/dev/null <<<"$out"'

# Operator-adopted and runtime-coupled skills are not fleet-published.
write_skill adopted-skill "" operator-adopt
write_status adopted-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "operator-adopted skill is excluded" \
  '[ "$rc" = 0 ] && jq -e ".blocked[0].code == \"autosave_marker_invalid\"" >/dev/null <<<"$out"'

write_skill claude-local-skill '4. Inspect ~/.claude/state before continuing.'
write_status claude-local-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "runtime-coupled skill is excluded from shared promotion" \
  '[ "$rc" = 0 ] && jq -e ".blocked[0].code == \"runtime_specific_claude\"" >/dev/null <<<"$out"'

# Unsafe support paths cannot cross the publication boundary.
write_skill linked-skill ""
ln -s /etc/passwd "$CLAUDE_SKILLS/linked-skill/notes"
write_status linked-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "symlinked support content fails closed" \
  '[ "$rc" = 0 ] && jq -e ".blocked[0].code == \"source_symlink\"" >/dev/null <<<"$out"'

echo "skill-promotion tests: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
