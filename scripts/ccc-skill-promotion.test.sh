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

# Disabled is the safe default and never touches the ownership tool or GitHub.
printf '{"skills":[]}\n' > "$STATUS_JSON"
out="$(env "${base_env[@]}" python3 "$PROMOTER" status)"; rc=$?
ok "disabled status is read-only and successful" \
  '[ "$rc" = 0 ] && jq -e ".ok and (.enabled == false) and (.publisher_enabled == false)" >/dev/null <<<"$out"'

# A node-local run only plans/stages owner-only envelopes.
write_skill release-checklist ""
write_status release-checklist
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "dry-run plans local outbox staging, not a PR" \
  '[ "$rc" = 0 ] && jq -e ".staged[0].outcome == \"would-stage-private-outbox\" and .staged[0].name == \"release-checklist\"" >/dev/null <<<"$out"'
ok "dry-run creates no ledger or outbox" \
  '[ ! -e "$STATE/skill-promotion/ledger.jsonl" ] && [ ! -d "$STATE/skill-promotion/outbox" ]'
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true CCC_AUTONOMY=dry-run python3 "$PROMOTER" run)"; rc=$?
ok "node staging honors fleet autonomy dry-run" \
  '[ "$rc" = 0 ] && jq -e ".mode == \"dry-run\" and .staged[0].outcome == \"would-stage-private-outbox\"" >/dev/null <<<"$out"'
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true CCC_AUTONOMY=kill python3 "$PROMOTER" run)"; rc=$?
ok "node staging honors fleet autonomy kill" \
  '[ "$rc" = 0 ] && jq -e ".status == \"autonomy-kill\" and .staged == []" >/dev/null <<<"$out"'

# GitHub and SSH stubs make the private publication path hermetic.
GH_STATE="$TMP/gh-state"
write_exec_stub "$BIN/gh" <<'SH'
set -eu
mkdir -p "$GH_TEST_STATE"
printf '%s\n' "$*" >> "$GH_TEST_STATE/calls"
case "${1:-} ${2:-}" in
  "auth status") exit 0 ;;
  "repo view")
    if [ "${GH_TEST_PRIVATE:-true}" = "true" ]; then
      printf '{"isPrivate":true,"visibility":"PRIVATE"}\n'
    else
      printf '{"isPrivate":false,"visibility":"PUBLIC"}\n'
    fi
    ;;
  "pr list")
    head=""
    previous=""
    for argument in "$@"; do
      [ "$previous" = "--head" ] && head="$argument"
      previous="$argument"
    done
    key="$(printf '%s' "$head" | sha256sum | awk '{print $1}')"
    if [ -f "$GH_TEST_STATE/created-$key" ]; then
      printf '[{"url":"https://github.com/test/repo/pull/1","state":"OPEN","isDraft":true}]\n'
    else
      printf '[]\n'
    fi
    ;;
  "pr create")
    head=""
    previous=""
    for argument in "$@"; do
      [ "$previous" = "--head" ] && head="$argument"
      previous="$argument"
    done
    key="$(printf '%s' "$head" | sha256sum | awk '{print $1}')"
    printf '%s\n' "$*" >> "$GH_TEST_STATE/create.args"
    : > "$GH_TEST_STATE/created-$key"
    printf 'https://github.com/test/repo/pull/1\n'
    ;;
  *) exit 9 ;;
esac
SH

write_exec_stub "$BIN/ssh" <<'SH'
set -eu
printf '%s\n' "$*" >> "$SSH_TEST_STATE/calls"
case " $* " in
  *" export "*) cat "$SSH_TEST_EXPORT" ;;
  *" ack "*) printf '{"ok":true,"acked":true}\n' ;;
  *) exit 8 ;;
esac
SH

stage_env=(
  "${base_env[@]}"
  "CCC_SKILL_PROMOTION_ENABLED=true"
  "GH_TEST_STATE=$GH_STATE"
  "PATH=$BIN:$PATH"
)
out="$(env "${stage_env[@]}" python3 "$PROMOTER" run)"; rc=$?
outbox_file="$(find "$STATE/skill-promotion/outbox" -maxdepth 1 -type f -name '*.json' | head -1)"
ok "live node run stages one owner-only envelope" \
  '[ "$rc" = 0 ] && jq -e ".staged[0].outcome == \"staged\"" >/dev/null <<<"$out" && [ "$(stat -c %a "$outbox_file")" = 600 ]'
ok "node-local staging makes no GitHub or SSH call" \
  '[ ! -e "$GH_STATE/calls" ] && [ ! -e "$TMP/ssh-state/calls" ]'
out="$(env "${stage_env[@]}" python3 "$PROMOTER" export --limit 1)"; rc=$?
transport_id="$(jq -r '.envelopes[0].transport_id' <<<"$out")"
ok "read-only export returns a hash-verified envelope" \
  '[ "$rc" = 0 ] && jq -e ".mode == \"export-read-only\" and .envelopes[0].node == \"testnode\" and (.envelopes[0].files[0].content_b64 | length > 20)" >/dev/null <<<"$out"'

# Existing private repository scaffold; raw intake will be written only on a branch.
SEED="$TMP/seed"
REMOTE="$TMP/remote.git"
mkdir -p "$SEED/approved/shared" "$SEED/approved/claude" "$SEED/approved/codex"
printf '\n' > "$SEED/approved/shared/.gitkeep"
printf '\n' > "$SEED/approved/claude/.gitkeep"
printf '\n' > "$SEED/approved/codex/.gitkeep"
git -C "$SEED" init -q -b main
git -C "$SEED" -c user.name=test -c user.email=test@example.invalid add .
git -C "$SEED" -c user.name=test -c user.email=test@example.invalid commit -qm seed
git clone -q --bare "$SEED" "$REMOTE"

publish_env=(
  "${stage_env[@]}"
  "CCC_SKILL_PROMOTION_PUBLISHER=true"
  "CCC_SKILL_PROMOTION_REMOTE=$REMOTE"
  "GH_TEST_PRIVATE=true"
)
out="$(env "${publish_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
branch="$(jq -r '.published[0].branch' <<<"$out")"
candidate_id="release-checklist-$(jq -r '.published[0].tree_sha256[0:12]' <<<"$out")"
ok "central publisher opens a private draft intake PR" \
  '[ "$rc" = 0 ] && jq -e ".published[0].outcome == \"pr-opened\" and .published[0].draft == \"true\"" >/dev/null <<<"$out" && grep -q -- "--draft" "$GH_STATE/create.args"'
ok "intake branch contains manifest and candidate but no public catalog mutation" \
  'git --git-dir="$REMOTE" show "$branch:intake/testnode/claude/$candidate_id/manifest.json" | jq -e ".node == \"testnode\" and .provider == \"claude\"" >/dev/null && git --git-dir="$REMOTE" show "$branch:intake/testnode/claude/$candidate_id/skill/SKILL.md" >/dev/null && ! git --git-dir="$REMOTE" cat-file -e "$branch:skills/shared/release-checklist/SKILL.md" 2>/dev/null && ! git --git-dir="$REMOTE" cat-file -e "$branch:codex/compatibility.json" 2>/dev/null'
ok "successful collection retains the envelope in sent and clears export" \
  '[ -f "$STATE/skill-promotion/sent/$transport_id.json" ] && [ ! -e "$STATE/skill-promotion/outbox/$transport_id.json" ] && [ "$(stat -c %a "$STATE/skill-promotion/ledger.jsonl")" = 600 ] && [ "$(env "${stage_env[@]}" python3 "$PROMOTER" export --limit 1 | jq ".envelopes | length")" = 0 ]'

# A public target is refused before either a git push or remote-node SSH export.
write_skill visibility-check ""
write_status visibility-check
env "${stage_env[@]}" python3 "$PROMOTER" run >/dev/null
remote_before="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname)' refs/heads | wc -l)"
SSH_STATE="$TMP/ssh-state"
mkdir -p "$SSH_STATE"
out="$(env "${publish_env[@]}" GH_TEST_PRIVATE=false \
  CCC_SKILL_PROMOTION_COLLECT_NODES=remotenode SSH_TEST_STATE="$SSH_STATE" \
  SSH_TEST_EXPORT="$TMP/unused" python3 "$PROMOTER" collect)"; rc=$?
remote_after="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname)' refs/heads | wc -l)"
ok "public repository visibility fails closed before data leaves the publisher" \
  '[ "$rc" = 2 ] && jq -e ".code == \"target_repo_not_private\"" >/dev/null <<<"$out" && [ "$remote_before" = "$remote_after" ] && [ ! -e "$SSH_STATE/calls" ]'
visibility_transport="$(env "${stage_env[@]}" python3 "$PROMOTER" export --limit 1 | jq -r '.envelopes[0].transport_id')"
env "${stage_env[@]}" python3 "$PROMOTER" ack "$visibility_transport" >/dev/null

# A remote node export is accepted only when its claimed node matches the SSH alias.
write_skill remote-check ""
write_status remote-check
remote_stage_env=("${stage_env[@]}" "CCC_NODE=remotenode")
env "${remote_stage_env[@]}" python3 "$PROMOTER" run >/dev/null
REMOTE_EXPORT="$TMP/remote-export.json"
env "${remote_stage_env[@]}" python3 "$PROMOTER" export --limit 1 > "$REMOTE_EXPORT"
remote_transport="$(jq -r '.envelopes[0].transport_id' "$REMOTE_EXPORT")"
env "${remote_stage_env[@]}" python3 "$PROMOTER" ack "$remote_transport" >/dev/null
REMOTE_GH_STATE="$TMP/remote-gh-state"
REMOTE_SSH_STATE="$TMP/remote-ssh-state"
mkdir -p "$REMOTE_SSH_STATE"
remote_collect_env=(
  "${publish_env[@]}"
  "GH_TEST_STATE=$REMOTE_GH_STATE"
  "CCC_SKILL_PROMOTION_COLLECT_NODES=remotenode"
  "SSH_TEST_STATE=$REMOTE_SSH_STATE"
  "SSH_TEST_EXPORT=$REMOTE_EXPORT"
)
out="$(env "${remote_collect_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "central collector publishes a matching remote-node envelope" \
  '[ "$rc" = 0 ] && jq -e ".published[0].source == \"remotenode\" and .published[0].node == \"remotenode\" and .published[0].outcome == \"pr-opened\"" >/dev/null <<<"$out"'
ok "remote candidate is acknowledged only after PR publication" \
  'grep -q " ack $remote_transport" "$REMOTE_SSH_STATE/calls"'

# Fresh scans continue to fail closed before any envelope is staged.
write_skill leaky-skill '4. token=abcdefghijklmnop1234567890'
write_status leaky-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "credential-shaped content stays local and unstaged" \
  '[ "$rc" = 0 ] && jq -e ".staged == [] and .blocked[0].code == \"secret_credential-assignment\"" >/dev/null <<<"$out"'

write_skill adopted-skill "" operator-adopt
write_status adopted-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "operator-adopted skill is excluded" \
  '[ "$rc" = 0 ] && jq -e ".blocked[0].code == \"autosave_marker_invalid\"" >/dev/null <<<"$out"'

write_skill claude-local-skill '4. Inspect ~/.claude/state before continuing.'
write_status claude-local-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "runtime-coupled skill is excluded from automatic intake" \
  '[ "$rc" = 0 ] && jq -e ".blocked[0].code == \"runtime_specific_claude\"" >/dev/null <<<"$out"'

write_skill linked-skill ""
ln -s /etc/passwd "$CLAUDE_SKILLS/linked-skill/notes"
write_status linked-skill
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "symlinked support content fails closed" \
  '[ "$rc" = 0 ] && jq -e ".blocked[0].code == \"source_symlink\"" >/dev/null <<<"$out"'

# --- #1067: an unresolvable fleet identity must fail closed -------------------
# `bash -lc`, which the autosave cron uses, exports neither CCC_NODE nor
# HOSTNAME. Staging under a placeholder name produced envelopes that every
# publisher rejected as remote_node_mismatch, while the node reported success.
no_node_env=()
for entry in "${base_env[@]}"; do
  case "$entry" in CCC_NODE=*) continue;; esac
  no_node_env+=("$entry")
done

write_skill identity-guard ""
write_status identity-guard
out="$(env -u CCC_NODE -u HOSTNAME "${no_node_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run)"; rc=$?
ok "no CCC_NODE/HOSTNAME: run fails closed instead of staging a placeholder" \
  '[ "$rc" != 0 ] && jq -e ".ok == false and .code == \"node_identity_unresolved\"" >/dev/null <<<"$out"'
ok "no CCC_NODE/HOSTNAME: nothing is written to the outbox" \
  '[ ! -e "$STATE/skill-promotion/outbox" ] || [ -z "$(ls -A "$STATE/skill-promotion/outbox")" ]'

# The guard must reject a name that sanitizes away, not just an unset variable.
out="$(env "${no_node_env[@]}" CCC_NODE=' /// ' CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run)"; rc=$?
ok "CCC_NODE that sanitizes to empty also fails closed" \
  '[ "$rc" != 0 ] && jq -e ".code == \"node_identity_unresolved\"" >/dev/null <<<"$out"'

# HOSTNAME alone still resolves — the guard rejects absence, not the fallback.
out="$(env "${no_node_env[@]}" HOSTNAME=fallbacknode CCC_SKILL_PROMOTION_ENABLED=true python3 "$PROMOTER" run --dry-run)"; rc=$?
ok "HOSTNAME alone still resolves an identity" \
  '[ "$rc" = 0 ] && jq -e ".staged[0].transport_id | startswith(\"fallbacknode-\")" >/dev/null <<<"$out"'

# ---- Termux-shaped ancestors above the trust root (#1069) ------------------
# On Android /data and /data/data are 771 system(1000): platform-owned and not
# changeable by the app. Walking to / rejected every correctly-provisioned
# Termux path, and because _read_enabled_file runs the same check the node just
# reported enabled:false with nothing anywhere saying why — daegyo and gongyung
# were structurally excluded from intake. The fixture reproduces the mode half
# of that (group/other-writable ancestors); the uid half needs root to build and
# is rejected by the same branch.
droid_root="$TMP/droid"
droid_home="$droid_root/data/data/com.termux/files/home"
droid_state="$droid_home/.claude/state"
mkdir -p "$droid_state" "$droid_home/.claude/skills" "$droid_home/.codex/skills"
chmod 700 "$droid_home" "$droid_home/.claude" "$droid_state" \
  "$droid_home/.claude/skills" "$droid_home/.codex" "$droid_home/.codex/skills"
chmod 0777 "$droid_root/data" "$droid_root/data/data"
printf 'true\n' > "$droid_state/skill-promotion.enabled"
chmod 600 "$droid_state/skill-promotion.enabled"
printf '{"skills":[]}\n' > "$STATUS_JSON"
droid_env=(
  "HOME=$droid_home"
  "CCC_CLAUDE_DIR=$droid_home/.claude"
  "CCC_STATE_DIR=$droid_state"
  "CCC_SKILL_PROMOTION_OWNERSHIP_TOOL=$OWNERSHIP"
  "CCC_SKILL_PROMOTION_CLAUDE_SKILLS_DIR=$droid_home/.claude/skills"
  "CCC_SKILL_PROMOTION_CODEX_SKILLS_DIR=$droid_home/.codex/skills"
  "CCC_SKILL_PROMOTION_PROVIDERS=claude"
  "CCC_SKILL_PROMOTION_REPO=test/repo"
  "CCC_NODE=droidnode"
  "PROMOTION_TEST_STATUS=$STATUS_JSON"
)
out="$(env "${droid_env[@]}" python3 "$PROMOTER" status)"; rc=$?
ok "writable ancestors above the trust root no longer hide the enabled flag" \
  '[ "$rc" = 0 ] && jq -e ".enabled == true" >/dev/null <<<"$out"'

# The anchor relaxes ABOVE the root only. Everything from the root down is
# checked exactly as before, or this would be a hole rather than a fix.
chmod 0777 "$droid_state"
out="$(env "${droid_env[@]}" python3 "$PROMOTER" status)"; rc=$?
ok "a writable directory BELOW the trust root is still refused" \
  '[ "$rc" = 0 ] && jq -e ".enabled == false" >/dev/null <<<"$out"'
chmod 700 "$droid_state"

# A trust root that fails the rules is not a trust root: fall back to the walk
# from /, which then rejects the writable ancestors again.
chmod 0777 "$droid_home"
out="$(env "${droid_env[@]}" python3 "$PROMOTER" status)"; rc=$?
ok "a writable trust root grants nothing" \
  '[ "$rc" = 0 ] && jq -e ".enabled == false" >/dev/null <<<"$out"'
chmod 700 "$droid_home"

# A state dir outside $HOME is a legitimate configuration (CCC_STATE_DIR is a
# documented override). The anchor must not apply there, and must not refuse it
# either — that would silently break a valid node, the same class of failure
# this fix removes.
outside_state="$TMP/outside-state"
mkdir -p "$outside_state"
chmod 700 "$outside_state"
printf 'true\n' > "$outside_state/skill-promotion.enabled"
chmod 600 "$outside_state/skill-promotion.enabled"
out="$(env "${base_env[@]}" CCC_STATE_DIR="$outside_state" python3 "$PROMOTER" status)"; rc=$?
ok "a state dir outside HOME still resolves through the unanchored walk" \
  '[ "$rc" = 0 ] && jq -e ".enabled == true" >/dev/null <<<"$out"'

echo "skill-promotion tests: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
