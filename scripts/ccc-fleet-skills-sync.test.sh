#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SYNC="$HERE/ccc-fleet-skills-sync.py"
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

SEED="$TMP/seed"
REMOTE="$TMP/remote.git"
SKILL="$SEED/approved/shared/release-checklist"
mkdir -p "$SKILL" "$SEED/approved/claude" "$SEED/approved/codex"
printf -- '---\nname: release-checklist\ndescription: Verify a reusable release with bounded and reviewable checks.\n---\n\n# Release checklist\n\n1. Inspect the intended change.\n2. Run the relevant checks.\n3. Record the result.\n' > "$SKILL/SKILL.md"
jq -n '{schema_version:1,source_candidate_id:"release-checklist-000000000000",
  source_tree_sha256:("0" * 64),approved_at:"2026-08-09T00:00:00Z",
  reviewed_by:"independent-reviewer"}' > "$SKILL/approval.json"
git -C "$SEED" init -q -b main
git -C "$SEED" -c user.name=test -c user.email=test@example.invalid add .
git -C "$SEED" -c user.name=test -c user.email=test@example.invalid commit -qm seed
REF="$(git -C "$SEED" rev-parse HEAD)"
git clone -q --bare "$SEED" "$REMOTE"

BIN="$TMP/bin"
GH_STATE="$TMP/gh-state"
mkdir -p "$BIN" "$GH_STATE"
write_exec_stub "$BIN/gh" <<'SH'
set -eu
printf '%s\n' "$*" >> "$GH_SYNC_STATE/calls"
case "${1:-} ${2:-}" in
  "auth status") exit 0 ;;
  "repo view")
    if [ "${GH_SYNC_PRIVATE:-true}" = true ]; then
      printf '{"isPrivate":true,"visibility":"PRIVATE"}\n'
    else
      printf '{"isPrivate":false,"visibility":"PUBLIC"}\n'
    fi
    ;;
  *) exit 9 ;;
esac
SH

HOME_DIR="$TMP/home"
CLAUDE_ROOT="$HOME_DIR/.claude/skills"
CODEX_ROOT="$HOME_DIR/.codex/skills"
STATE="$HOME_DIR/.claude/state/fleet-skills"
mkdir -p "$CLAUDE_ROOT" "$CODEX_ROOT" "$HOME_DIR/.claude/state"
chmod 700 "$HOME_DIR" "$HOME_DIR/.claude" "$HOME_DIR/.claude/state" \
  "$HOME_DIR/.codex" "$CLAUDE_ROOT" "$CODEX_ROOT"
base_env=(
  "HOME=$HOME_DIR"
  "CCC_FLEET_SKILLS_STATE_DIR=$STATE"
  "CCC_FLEET_SKILLS_CLAUDE_DIR=$CLAUDE_ROOT"
  "CCC_FLEET_SKILLS_CODEX_DIR=$CODEX_ROOT"
  "CCC_FLEET_SKILLS_REPO=test/repo"
  "CCC_FLEET_SKILLS_REMOTE=$REMOTE"
  "GH_SYNC_STATE=$GH_STATE"
  "PATH=$BIN:$PATH"
)

out="$(env "${base_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "exact private commit plans shared skill for both providers" \
  '[ "$rc" = 0 ] && jq -e "(.operations | length) == 2 and (.operations | all(.action == \"install\")) and ([.operations[].provider] | sort) == [\"claude\",\"codex\"]" >/dev/null <<<"$out"'
ok "plan does not create installed targets" \
  '[ ! -e "$CLAUDE_ROOT/release-checklist" ] && [ ! -e "$CODEX_ROOT/release-checklist" ]'

out="$(env "${base_env[@]}" python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "apply atomically installs both provider copies" \
  '[ "$rc" = 0 ] && jq -e ".changed == 2" >/dev/null <<<"$out" && cmp -s "$SKILL/SKILL.md" "$CLAUDE_ROOT/release-checklist/SKILL.md" && cmp -s "$SKILL/SKILL.md" "$CODEX_ROOT/release-checklist/SKILL.md"'
ok "installed copies carry owner-only exact-commit provenance" \
  '[ "$(stat -c %a "$CLAUDE_ROOT/release-checklist/.ccc-fleet-skill.json")" = 600 ] && jq -e --arg ref "$REF" ".manager == \"ccc-node-fleet-skills\" and .commit == \$ref and .provider == \"claude\"" "$CLAUDE_ROOT/release-checklist/.ccc-fleet-skill.json" >/dev/null'
ok "receipt is body-free and owner-only" \
  '[ "$(stat -c %a "$STATE/installed.json")" = 600 ] && jq -e --arg ref "$REF" ".commit == \$ref and (.skills | length) == 2" "$STATE/installed.json" >/dev/null && ! grep -q "Inspect the intended" "$STATE/installed.json"'

out="$(env "${base_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "exact-commit rerun converges to noops" \
  '[ "$rc" = 0 ] && jq -e ".operations | all(.action == \"noop\")" >/dev/null <<<"$out"'

printf '\nlocal drift\n' >> "$CLAUDE_ROOT/release-checklist/SKILL.md"
out="$(env "${base_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "managed content drift is detected as an update" \
  '[ "$rc" = 0 ] && jq -e ".operations | any(.provider == \"claude\" and .action == \"update\")" >/dev/null <<<"$out"'

CONFLICT_HOME="$TMP/conflict-home"
CONFLICT_CLAUDE="$CONFLICT_HOME/.claude/skills"
CONFLICT_CODEX="$CONFLICT_HOME/.codex/skills"
mkdir -p "$CONFLICT_CLAUDE/release-checklist" "$CONFLICT_CODEX"
chmod 700 "$CONFLICT_HOME" "$CONFLICT_HOME/.claude" "$CONFLICT_HOME/.codex" \
  "$CONFLICT_CLAUDE" "$CONFLICT_CODEX" "$CONFLICT_CLAUDE/release-checklist"
printf 'user owned\n' > "$CONFLICT_CLAUDE/release-checklist/notes"
out="$(env "${base_env[@]}" HOME="$CONFLICT_HOME" \
  CCC_FLEET_SKILLS_STATE_DIR="$CONFLICT_HOME/.claude/state/fleet-skills" \
  CCC_FLEET_SKILLS_CLAUDE_DIR="$CONFLICT_CLAUDE" \
  CCC_FLEET_SKILLS_CODEX_DIR="$CONFLICT_CODEX" \
  python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "user-owned target conflict blocks the entire apply" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_user_owned\"" >/dev/null <<<"$out" && [ ! -e "$CONFLICT_CODEX/release-checklist" ]'

out="$(env "${base_env[@]}" GH_SYNC_PRIVATE=false python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "public repository is refused before clone or install" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_repo_not_private\"" >/dev/null <<<"$out"'

out="$(env "${base_env[@]}" python3 "$SYNC" plan --ref main)"; rc=$?
ok "floating main ref is refused" \
  '[ "$rc" = 2 ] && jq -e ".code == \"exact_commit_required\"" >/dev/null <<<"$out"'


# Graduation precedence (#1344): repo-managed(setup) > fleet-approved(sync).
# A repo-managed target is skipped, never fought; the lower-precedence layer
# must converge to a no-op while the higher layer owns the directory.
GRAD_HOME="$TMP/grad-home"
GRAD_CLAUDE="$GRAD_HOME/.claude/skills"
GRAD_CODEX="$GRAD_HOME/.codex/skills"
mkdir -p "$GRAD_CLAUDE/release-checklist" "$GRAD_CODEX" "$GRAD_HOME/.claude/state"
chmod 700 "$GRAD_HOME" "$GRAD_HOME/.claude" "$GRAD_HOME/.claude/state" "$GRAD_HOME/.codex" "$GRAD_CLAUDE" "$GRAD_CODEX" "$GRAD_CLAUDE/release-checklist"
printf 'pre-graduation fleet-era copy\n' > "$GRAD_CLAUDE/release-checklist/SKILL.md"
printf 'release-checklist 0\n' > "$GRAD_HOME/.claude/state/repo-skills.manifest"
grad_env=("${base_env[@]}" HOME="$GRAD_HOME" \
  CCC_FLEET_SKILLS_STATE_DIR="$GRAD_HOME/.claude/state/fleet-skills" \
  CCC_FLEET_SKILLS_CLAUDE_DIR="$GRAD_CLAUDE" \
  CCC_FLEET_SKILLS_CODEX_DIR="$GRAD_CODEX")

# 1) claude-side repo ownership comes from setup.sh's repo-skills.manifest.
out="$(env "${grad_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "graduated plan skips repo-managed claude target and keeps codex install" \
  '[ "$rc" = 0 ] && jq -e "(.operations | any(.provider == \"claude\" and .action == \"skip-repo-managed\")) and (.operations | any(.provider == \"codex\" and .action == \"install\"))" >/dev/null <<<"$out"'

# 2) end-to-end graduation sequence: fleet-install -> the repo layer takes
#    ownership (setup absorbs: repo bytes + manifest entry; the codex
#    provisioner markers its copy) -> sync skips both, mutating nothing.
out="$(env "${grad_env[@]}" python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "graduated apply installs only the codex copy" \
  '[ "$rc" = 0 ] && jq -e ".changed == 1" >/dev/null <<<"$out"'
printf 'repo-managed copy\n' > "$GRAD_CLAUDE/release-checklist/SKILL.md"
rm -f "$GRAD_CLAUDE/release-checklist/.ccc-fleet-skill.json"
repo_hash="$(cd "$GRAD_CLAUDE/release-checklist" && find . -type f -exec sha256sum {} + | LC_ALL=C sort -k2 | sha256sum | awk '{print $1}')"
printf 'release-checklist %s\n' "$repo_hash" > "$GRAD_HOME/.claude/state/repo-skills.manifest"
jq -n '{manager:"ccc-node",name:"release-checklist"}' > "$GRAD_CODEX/release-checklist/.ccc-node-managed.json"
rm -f "$GRAD_CODEX/release-checklist/.ccc-fleet-skill.json"
sha_before_claude="$(sha256sum "$GRAD_CLAUDE/release-checklist/SKILL.md" | awk '{print $1}')"
sha_before_codex="$(sha256sum "$GRAD_CODEX/release-checklist/SKILL.md" | awk '{print $1}')"

out="$(env "${grad_env[@]}" python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "sync after graduation skips both repo-managed targets without error" \
  '[ "$rc" = 0 ] && jq -e "(.changed == 0) and (.operations | all(.action == \"skip-repo-managed\"))" >/dev/null <<<"$out"'
sha_after_claude="$(sha256sum "$GRAD_CLAUDE/release-checklist/SKILL.md" | awk '{print $1}')"
sha_after_codex="$(sha256sum "$GRAD_CODEX/release-checklist/SKILL.md" | awk '{print $1}')"
ok "repo-managed copies are byte-untouched by the skip" \
  '[ "$sha_after_claude" = "$sha_before_claude" ] && [ "$sha_after_codex" = "$sha_before_codex" ]'

# 3) tampered repo markers still fail closed — skip is for genuine ownership
#    by the repo layer, and a wrong-manager marker is refused, not skipped.
jq -n '{manager:"intruder",name:"release-checklist"}' > "$GRAD_CODEX/release-checklist/.ccc-node-managed.json"
out="$(env "${grad_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "tampered repo-managed marker fails closed" \
  '[ "$rc" = 2 ] && jq -e ".code == \"repo_managed_marker_invalid\"" >/dev/null <<<"$out"'

# ─── 4) approval.json without reviewed_by is valid (#2030) ─────────────────
#    The owner decision in a2a-nexus#2030 retires the human reviewed_by gate;
#    approvals omit the field, installed markers carry null, and reruns stay
#    idempotent.
SEED2="$TMP/seed-norev"
REMOTE2="$TMP/remote-norev.git"
SKILL2="$SEED2/approved/shared/minimal-skill"
mkdir -p "$SKILL2" "$SEED2/approved/claude" "$SEED2/approved/codex"
printf -- '---\nname: minimal-skill\ndescription: Approve without a human reviewer.\n---\n\n# Minimal skill\n\n1. Do the step.\n2. Record the result.\n' > "$SKILL2/SKILL.md"
jq -n '{schema_version:1,source_candidate_id:"minimal-skill-000000000000",
  source_tree_sha256:("0" * 64),approved_at:"2026-09-01T00:00:00Z"}' > "$SKILL2/approval.json"
git -C "$SEED2" init -q -b main
git -C "$SEED2" -c user.name=test -c user.email=test@example.invalid add .
git -C "$SEED2" -c user.name=test -c user.email=test@example.invalid commit -qm seed
REF2="$(git -C "$SEED2" rev-parse HEAD)"
git clone -q --bare "$SEED2" "$REMOTE2"

HOME_DIR2="$TMP/home-norev"
CLAUDE_ROOT2="$HOME_DIR2/.claude/skills"
CODEX_ROOT2="$HOME_DIR2/.codex/skills"
STATE2="$HOME_DIR2/.claude/state/fleet-skills"
mkdir -p "$CLAUDE_ROOT2" "$CODEX_ROOT2" "$HOME_DIR2/.claude/state"
chmod 700 "$HOME_DIR2" "$HOME_DIR2/.claude" "$HOME_DIR2/.claude/state" \
  "$HOME_DIR2/.codex" "$CLAUDE_ROOT2" "$CODEX_ROOT2"
norev_env=(
  "HOME=$HOME_DIR2"
  "CCC_FLEET_SKILLS_STATE_DIR=$STATE2"
  "CCC_FLEET_SKILLS_CLAUDE_DIR=$CLAUDE_ROOT2"
  "CCC_FLEET_SKILLS_CODEX_DIR=$CODEX_ROOT2"
  "CCC_FLEET_SKILLS_REPO=test/repo"
  "CCC_FLEET_SKILLS_REMOTE=$REMOTE2"
  "GH_SYNC_STATE=$GH_STATE"
  "PATH=$BIN:$PATH"
)

out="$(env "${norev_env[@]}" python3 "$SYNC" apply --ref "$REF2")"; rc=$?
ok "approval.json without reviewed_by installs (#2030)" \
  '[ "$rc" = 0 ] && jq -e ".changed == 2" >/dev/null <<<"$out" && jq -e '"'"'.reviewed_by == null'"'"' "$CLAUDE_ROOT2/minimal-skill/.ccc-fleet-skill.json" >/dev/null'

out="$(env "${norev_env[@]}" python3 "$SYNC" plan --ref "$REF2")"; rc=$?
ok "no-reviewed_by install converges to noops" \
  '[ "$rc" = 0 ] && jq -e '"'"'.operations | all(.action == "noop")'"'"' >/dev/null <<<"$out"'

# ─── 5) Termux app-private root walk (#1390) ──────────────────────────────
#    Android /data and /data/data are system-owned 0771 and the Termux app
#    root's files/ is 0771 by bootstrap default, so the strict anchor walk
#    rejected every state/skills path on gongyung/daegyo. When the validated
#    app-private root (kernel-enforced, only app uid + root traverse) lies
#    on the path, the walk starts there; hostile parents above it are
#    skipped and group/other-write bits below it stop mattering. The final
#    state dir stays fully strict, and nothing outside the root relaxes.
ANDROID="$TMP/android"
APP_ROOT="$ANDROID/data/data/com.termux"
TERMUX_HOME="$APP_ROOT/files/home"
T_CLAUDE="$TERMUX_HOME/.claude/skills"
T_CODEX="$TERMUX_HOME/.codex/skills"
T_STATE="$TERMUX_HOME/.claude/state/fleet-skills"
mkdir -p "$APP_ROOT/files" "$T_CLAUDE" "$T_CODEX" "$TERMUX_HOME/.claude/state"
chmod 777 "$ANDROID" "$ANDROID/data" "$ANDROID/data/data"
chmod 700 "$APP_ROOT"
chmod 771 "$APP_ROOT/files"
chmod 700 "$TERMUX_HOME" "$TERMUX_HOME/.claude" "$TERMUX_HOME/.claude/state" \
  "$T_CLAUDE" "$T_CODEX"
termux_env=("${base_env[@]}"
  "CCC_FLEET_SKILLS_APP_ROOT=$APP_ROOT"
  "HOME=$TERMUX_HOME"
  "CCC_FLEET_SKILLS_STATE_DIR=$T_STATE"
  "CCC_FLEET_SKILLS_CLAUDE_DIR=$T_CLAUDE"
  "CCC_FLEET_SKILLS_CODEX_DIR=$T_CODEX"
)

out="$(env "${termux_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "termux plan passes hostile system-owned parents (#1390)" \
  '[ "$rc" = 0 ] && jq -e "(.operations | length) == 2 and (.operations | all(.action == \"install\"))" >/dev/null <<<"$out"'

out="$(env "${termux_env[@]}" python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "termux apply installs both provider copies (#1390)" \
  '[ "$rc" = 0 ] && jq -e ".changed == 2" >/dev/null <<<"$out" && cmp -s "$SKILL/SKILL.md" "$T_CLAUDE/release-checklist/SKILL.md"'
ok "termux state receipt is owner-only" \
  '[ "$(stat -c %a "$T_STATE/installed.json")" = 600 ]'

chmod 711 "$APP_ROOT"
out="$(env "${termux_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "group/other-traversable app root keeps the strict anchor walk" \
  '[ "$rc" = 2 ] && jq -e ".code == \"state_path_unsafe\"" >/dev/null <<<"$out"'
chmod 700 "$APP_ROOT"

mv "$APP_ROOT" "$APP_ROOT.real"
ln -s "$ANDROID/data" "$APP_ROOT"
out="$(env "${termux_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "symlinked app root keeps the strict anchor walk" \
  '[ "$rc" = 2 ] && jq -e ".code == \"state_path_unsafe\"" >/dev/null <<<"$out"'
rm "$APP_ROOT"
mv "$APP_ROOT.real" "$APP_ROOT"

OPEN="$TMP/open-state-parent"
mkdir -p "$OPEN/state"
chmod 777 "$OPEN"
out="$(env "${base_env[@]}" CCC_FLEET_SKILLS_APP_ROOT="$APP_ROOT" \
  CCC_FLEET_SKILLS_STATE_DIR="$OPEN/state" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "world-writable parent outside the app root still fails" \
  '[ "$rc" = 2 ] && jq -e ".code == \"state_path_unsafe\"" >/dev/null <<<"$out"'

chmod 750 "$T_STATE"
out="$(env "${termux_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "final state dir below the app root stays strictly private" \
  '[ "$rc" = 2 ] && jq -e ".code == \"state_path_unsafe\"" >/dev/null <<<"$out"'
chmod 700 "$T_STATE"

# ─── 6) retirement reconcile (#92) ──────────────────────────────
#    The repo dropping an approved skill must propagate: marker-carrying
#    installs whose tree still hashes to the marker are pruned (backed up),
#    locally edited or marker-tampered copies are kept and reported, and
#    markerless (user-owned) siblings are never touched.
RET_HOME="$TMP/retire-home"
RET_CLAUDE="$RET_HOME/.claude/skills"
RET_CODEX="$RET_HOME/.codex/skills"
RET_STATE="$RET_HOME/.claude/state/fleet-skills"
mkdir -p "$RET_CLAUDE" "$RET_CODEX" "$RET_HOME/.claude/state"
chmod 700 "$RET_HOME" "$RET_HOME/.claude" "$RET_HOME/.claude/state" \
  "$RET_HOME/.codex" "$RET_CLAUDE" "$RET_CODEX"
ret_env=("${base_env[@]}" HOME="$RET_HOME" \
  CCC_FLEET_SKILLS_STATE_DIR="$RET_STATE" \
  CCC_FLEET_SKILLS_CLAUDE_DIR="$RET_CLAUDE" \
  CCC_FLEET_SKILLS_CODEX_DIR="$RET_CODEX")

out="$(env "${ret_env[@]}" python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "retirement fixture installs the shared skill first" \
  '[ "$rc" = 0 ] && jq -e ".changed == 2" >/dev/null <<<"$out"'
mkdir -p "$RET_CLAUDE/user-own"
printf 'local
' > "$RET_CLAUDE/user-own/SKILL.md"

SEED3="$TMP/seed-empty"
REMOTE3="$TMP/remote-empty.git"
mkdir -p "$SEED3/approved/shared" "$SEED3/approved/claude" "$SEED3/approved/codex"
touch "$SEED3/approved/shared/.gitkeep" "$SEED3/approved/claude/.gitkeep" "$SEED3/approved/codex/.gitkeep"
git -C "$SEED3" init -q -b main
git -C "$SEED3" -c user.name=test -c user.email=test@example.invalid add .
git -C "$SEED3" -c user.name=test -c user.email=test@example.invalid commit -qm retire-all
REF3="$(git -C "$SEED3" rev-parse HEAD)"
git clone -q --bare "$SEED3" "$REMOTE3"

out="$(env "${ret_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE3" python3 "$SYNC" plan --ref "$REF3")"; rc=$?
ok "plan reports both unmodified installs as retire (#92)" \
  '[ "$rc" = 0 ] && jq -e "(.operations | length) == 0 and (.retirements | length) == 2 and (.retirements | all(.action == \"retire\")) and ([.retirements[].provider] | sort) == [\"claude\",\"codex\"]" >/dev/null <<<"$out"'
ok "plan does not prune and ignores the markerless sibling" \
  '[ -d "$RET_CLAUDE/release-checklist" ] && [ -d "$RET_CODEX/release-checklist" ] && [ ! -e "$RET_STATE/backups/$REF3" ]'

printf '\nlocal edits\n' >> "$RET_CLAUDE/release-checklist/SKILL.md"
out="$(env "${ret_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE3" python3 "$SYNC" plan --ref "$REF3")"; rc=$?
ok "drifted copy is kept and reported, pristine copy still retires" \
  '[ "$rc" = 0 ] && jq -e "(.retirements | any(.provider == \"claude\" and .action == \"retire-keep\")) and (.retirements | any(.provider == \"codex\" and .action == \"retire\"))" >/dev/null <<<"$out"'

out="$(env "${ret_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE3" python3 "$SYNC" apply --ref "$REF3")"; rc=$?
ok "apply prunes only the unmodified orphan" \
  '[ "$rc" = 0 ] && jq -e ".changed == 1" >/dev/null <<<"$out" && [ ! -e "$RET_CODEX/release-checklist" ] && [ -d "$RET_CLAUDE/release-checklist" ] && [ -f "$RET_CLAUDE/user-own/SKILL.md" ]'
ok "pruned copy is preserved under backups" \
  '[ -f "$RET_STATE/backups/$REF3/codex-release-checklist/SKILL.md" ]'
ok "receipt after retirement lists no skills" \
  '[ "$(stat -c %a "$RET_STATE/installed.json")" = 600 ] && jq -e "(.skills | length) == 0" >/dev/null <<<"$out"'

printf 'junk' > "$RET_CLAUDE/release-checklist/.ccc-fleet-skill.json"
out="$(env "${ret_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE3" python3 "$SYNC" plan --ref "$REF3")"; rc=$?
ok "tampered marker reads as drift: kept and reported, not fatal" \
  '[ "$rc" = 0 ] && jq -e "(.retirements | length) == 1 and (.retirements[0].provider == \"claude\") and (.retirements[0].action == \"retire-keep\")" >/dev/null <<<"$out"'
out="$(env "${ret_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE3" python3 "$SYNC" apply --ref "$REF3")"; rc=$?
ok "tampered-marker orphan is never deleted by apply" \
  '[ "$rc" = 0 ] && jq -e ".changed == 0" >/dev/null <<<"$out" && [ -d "$RET_CLAUDE/release-checklist" ]'

# ─── Piri third provider ────────────────────────────────────────────────
#    Piri joins only on nodes that already have ~/.piri/agent (setup.sh's
#    own gate for its piri skills install): shared skills install to all
#    three roots, audience-piri skills route to piri only, and non-Piri
#    nodes plan zero piri operations.
PIRI_HOME="$TMP/piri-home"
PIRI_ROOT="$PIRI_HOME/.piri/agent/skills"
PIRI_STATE="$PIRI_HOME/.claude/state/fleet-skills"
mkdir -p "$PIRI_HOME/.claude/skills" "$PIRI_HOME/.claude/state" \
  "$PIRI_HOME/.codex/skills" "$PIRI_HOME/.piri/agent/skills"
chmod 700 "$PIRI_HOME" "$PIRI_HOME/.claude" "$PIRI_HOME/.claude/state" \
  "$PIRI_HOME/.claude/skills" "$PIRI_HOME/.codex" "$PIRI_HOME/.codex/skills" \
  "$PIRI_HOME/.piri" "$PIRI_HOME/.piri/agent" "$PIRI_HOME/.piri/agent/skills"
piri_env=("HOME=$PIRI_HOME" \
  "CCC_FLEET_SKILLS_STATE_DIR=$PIRI_STATE" \
  "CCC_FLEET_SKILLS_REPO=test/repo" \
  "CCC_FLEET_SKILLS_REMOTE=$REMOTE" \
  "GH_SYNC_STATE=$GH_STATE" \
  "PATH=$BIN:$PATH")

out="$(env "${piri_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "piri node plans shared skill for all three providers" \
  '[ "$rc" = 0 ] && jq -e "(.operations | length) == 3 and ([.operations[].provider] | sort) == [\"claude\",\"codex\",\"piri\"]" >/dev/null <<<"$out"'

out="$(env "${piri_env[@]}" python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "piri apply installs the third provider copy with provenance" \
  '[ "$rc" = 0 ] && jq -e ".changed == 3" >/dev/null <<<"$out" && cmp -s "$SKILL/SKILL.md" "$PIRI_ROOT/release-checklist/SKILL.md" && jq -e ".provider == \"piri\"" "$PIRI_ROOT/release-checklist/.ccc-fleet-skill.json" >/dev/null'

out="$(env "${piri_env[@]}" python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "piri rerun converges to noops" \
  '[ "$rc" = 0 ] && jq -e ".operations | all(.action == \"noop\")" >/dev/null <<<"$out"'

# Piri user-owned (markerless) target fails closed like claude/codex.
PIRI2_HOME="$TMP/piri-conflict-home"
mkdir -p "$PIRI2_HOME/.claude/skills" "$PIRI2_HOME/.claude/state" \
  "$PIRI2_HOME/.codex/skills" "$PIRI2_HOME/.piri/agent/skills/release-checklist"
chmod 700 "$PIRI2_HOME" "$PIRI2_HOME/.claude" "$PIRI2_HOME/.claude/state" \
  "$PIRI2_HOME/.claude/skills" "$PIRI2_HOME/.codex" "$PIRI2_HOME/.codex/skills" \
  "$PIRI2_HOME/.piri" "$PIRI2_HOME/.piri/agent" "$PIRI2_HOME/.piri/agent/skills" \
  "$PIRI2_HOME/.piri/agent/skills/release-checklist"
printf 'user owned\n' > "$PIRI2_HOME/.piri/agent/skills/release-checklist/notes"
out="$(env "${piri_env[@]}" HOME="$PIRI2_HOME" \
  CCC_FLEET_SKILLS_STATE_DIR="$PIRI2_HOME/.claude/state/fleet-skills" \
  python3 "$SYNC" apply --ref "$REF")"; rc=$?
ok "piri user-owned target blocks the entire apply" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_user_owned\"" >/dev/null <<<"$out" && [ ! -e "$PIRI2_HOME/.claude/skills/release-checklist" ]'

# Piri repo-managed ownership comes from the piri setup manifest.
PIRI3_HOME="$TMP/piri-grad-home"
mkdir -p "$PIRI3_HOME/.claude/skills" "$PIRI3_HOME/.claude/state" \
  "$PIRI3_HOME/.codex/skills" "$PIRI3_HOME/.piri/agent/skills/release-checklist" \
  "$PIRI3_HOME/.piri/agent/state"
chmod 700 "$PIRI3_HOME" "$PIRI3_HOME/.claude" "$PIRI3_HOME/.claude/state" \
  "$PIRI3_HOME/.claude/skills" "$PIRI3_HOME/.codex" "$PIRI3_HOME/.codex/skills" \
  "$PIRI3_HOME/.piri" "$PIRI3_HOME/.piri/agent" "$PIRI3_HOME/.piri/agent/skills" \
  "$PIRI3_HOME/.piri/agent/skills/release-checklist" "$PIRI3_HOME/.piri/agent/state"
printf 'repo-managed copy\n' > "$PIRI3_HOME/.piri/agent/skills/release-checklist/SKILL.md"
printf 'release-checklist 0\n' > "$PIRI3_HOME/.piri/agent/state/repo-skills.manifest"
out="$(env "${piri_env[@]}" HOME="$PIRI3_HOME" \
  CCC_FLEET_SKILLS_STATE_DIR="$PIRI3_HOME/.claude/state/fleet-skills" \
  python3 "$SYNC" plan --ref "$REF")"; rc=$?
ok "piri repo-managed target is skipped; claude/codex still planned" \
  '[ "$rc" = 0 ] && jq -e "(.operations | any(.provider == \"piri\" and .action == \"skip-repo-managed\")) and (.operations | any(.provider == \"claude\" and .action == \"install\")) and (.operations | any(.provider == \"codex\" and .action == \"install\"))" >/dev/null <<<"$out"'

# Audience-piri skills route to piri only; non-Piri nodes plan nothing.
SEED2="$TMP/seed2"
REMOTE2="$TMP/remote2.git"
SKILL_PIRI="$SEED2/approved/piri/piri-only-tool"
mkdir -p "$SKILL_PIRI"
printf -- '---\nname: piri-only-tool\ndescription: A piri-audience skill for routing tests.\n---\n\n# Piri-only tool\n\n1. Do the piri thing.\n' > "$SKILL_PIRI/SKILL.md"
jq -n '{schema_version:1,source_candidate_id:"piri-only-tool-000000000000",
  source_tree_sha256:("0" * 64),approved_at:"2026-09-05T00:00:00Z"}' > "$SKILL_PIRI/approval.json"
git -C "$SEED2" init -q -b main
git -C "$SEED2" -c user.name=test -c user.email=test@example.invalid add .
git -C "$SEED2" -c user.name=test -c user.email=test@example.invalid commit -qm seed2
REF2B="$(git -C "$SEED2" rev-parse HEAD)"
git clone -q --bare "$SEED2" "$REMOTE2"

out="$(env "${piri_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE2" python3 "$SYNC" plan --ref "$REF2B")"; rc=$?
ok "audience-piri skill plans piri-only on a piri node" \
  '[ "$rc" = 0 ] && jq -e "(.operations | length) == 1 and .operations[0].provider == \"piri\" and .operations[0].action == \"install\"" >/dev/null <<<"$out"'

out="$(env "${base_env[@]}" CCC_FLEET_SKILLS_REMOTE="$REMOTE2" python3 "$SYNC" plan --ref "$REF2B")"; rc=$?
ok "audience-piri skill plans nothing on a non-piri node" \
  '[ "$rc" = 0 ] && jq -e "(.operations | length) == 0" >/dev/null <<<"$out"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
