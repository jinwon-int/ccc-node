#!/usr/bin/env bash
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROMOTER="$HERE/ccc-skill-promotion.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
# The promoter imports its sibling ccc_secure_fs (#1484); setup.sh installs
# both beside each other in hooks/. Drivers below load the promoter through
# importlib from other directories, so make the sibling importable once here.
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
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

# ─── #1477: collect / drop-report --ack share promotion.lock with run ─────────
# A second collect while another holds the lock must report status=locked and
# touch nothing (no PR, no ack, no ledger row); collect --dry-run is read-only
# and deliberately ignores the lock so a manual preview never starves behind
# the nightly collect waiting on CI.
write_skill lock-check ""
write_status lock-check
env "${stage_env[@]}" python3 "$PROMOTER" run >/dev/null
lock_transport="$(env "${stage_env[@]}" python3 "$PROMOTER" export --limit 1 | jq -r '.envelopes[0].transport_id')"
LOCK_HOLDER="$TMP/lock-holder.py"
cat > "$LOCK_HOLDER" <<'PY'
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
time.sleep(float(sys.argv[3]))
PY
LOCK_READY="$TMP/lock-ready"
rm -f "$LOCK_READY"
python3 "$LOCK_HOLDER" "$STATE/skill-promotion/promotion.lock" "$LOCK_READY" 90 &
lock_holder_pid=$!
# Bounded 30 s (600 x 50 ms): 5 s flaked once on a loaded CI shard (#1497).
# Fail loudly instead of silently running the locked-state assertions below
# against an unheld lock.
lock_ready=0
for _ in $(seq 1 600); do [ -e "$LOCK_READY" ] && { lock_ready=1; break; }; sleep 0.05; done
if [ "$lock_ready" != 1 ]; then
  echo "FAIL: #1477 lock-holder helper never signalled readiness within 30s (pid $lock_holder_pid)" >&2
  kill "$lock_holder_pid" 2>/dev/null; wait "$lock_holder_pid" 2>/dev/null
  exit 1
fi
LOCK_GH_STATE="$TMP/lock-gh-state"
lock_env=("${publish_env[@]}" "GH_TEST_STATE=$LOCK_GH_STATE")
ledger_lines_locked="$(grep -c . "$STATE/skill-promotion/ledger.jsonl")"
out="$(env "${lock_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "#1477: collect under a held promotion.lock reports locked (same shape as run)" \
  '[ "$rc" = 0 ] && jq -e ".ok and .mode == \"collect\" and .status == \"locked\" and .publisher_enabled and .published == [] and .errors == []" >/dev/null <<<"$out"'
ok "#1477: locked collect makes no GitHub call, no ack, no ledger row" \
  '[ ! -e "$LOCK_GH_STATE/calls" ] && [ -f "$STATE/skill-promotion/outbox/$lock_transport.json" ] && [ ! -e "$STATE/skill-promotion/sent/$lock_transport.json" ] && [ "$(grep -c . "$STATE/skill-promotion/ledger.jsonl")" = "$ledger_lines_locked" ]'
out="$(env "${stage_env[@]}" python3 "$PROMOTER" run)"; rc=$?
ok "#1477: run under a held promotion.lock still reports locked" \
  '[ "$rc" = 0 ] && jq -e ".ok and .mode == \"run\" and .status == \"locked\" and .staged == []" >/dev/null <<<"$out"'
out="$(env "${lock_env[@]}" python3 "$PROMOTER" collect --dry-run)"; rc=$?
ok "#1477: collect --dry-run is read-only and skips the lock" \
  '[ "$rc" = 0 ] && jq -e ".mode == \"collect-dry-run\" and (.status // \"\") != \"locked\" and .published[0].outcome == \"would-open-private-intake-pr\" and .published[0].name == \"lock-check\"" >/dev/null <<<"$out"'
ok "#1477: dry-run collect under the lock mutates nothing" \
  '[ -f "$STATE/skill-promotion/outbox/$lock_transport.json" ] && [ "$(grep -c . "$STATE/skill-promotion/ledger.jsonl")" = "$ledger_lines_locked" ] && ! grep -q "pr create" "$LOCK_GH_STATE/calls"'
out="$(env "${stage_env[@]}" python3 "$PROMOTER" drop-report --ack lock-task-x)"; rc=$?
ok "#1477: drop-report --ack under a held lock records nothing and says so" \
  '[ "$rc" = 0 ] && jq -e ".ok and .status == \"locked\" and .ack[0].task_id == \"lock-task-x\" and .ack[0].outcome == \"locked\" and .sweep_recorded == false" >/dev/null <<<"$out" && [ ! -e "$STATE/skill-promotion/drop-report.acked.jsonl" ]'
out="$(env "${stage_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "#1477: plain drop-report sweep stays viewable under a held lock" \
  '[ "$rc" = 0 ] && jq -e ".ok and (.status // \"\") != \"locked\" and .mode == \"drop-report\"" >/dev/null <<<"$out"'
kill "$lock_holder_pid" 2>/dev/null; wait "$lock_holder_pid" 2>/dev/null
out="$(env "${lock_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "#1477: collect proceeds once the lock is released" \
  '[ "$rc" = 0 ] && jq -e "(.status // \"\") != \"locked\" and .published[0].outcome == \"pr-opened\" and .published[0].name == \"lock-check\"" >/dev/null <<<"$out" && [ -f "$STATE/skill-promotion/sent/$lock_transport.json" ]'
ok "#1477: promotion.lock stays owner-only" \
  '[ "$(stat -c %a "$STATE/skill-promotion/promotion.lock")" = 600 ]'

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

# ─── R1 autorepair (#1357) ──────────────────────────────────────────────────

cat > "$TMP/ar-driver.py" <<'PYDRIVER'
import importlib.util, json, os, pathlib, sys
promoter, tmp, case_arg = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("promo_ar", promoter)
m = importlib.util.module_from_spec(spec); sys.modules["promo_ar"] = m; spec.loader.exec_module(m)
case = json.load(open(case_arg))
home = pathlib.Path(tmp) / "home"
env = {"HOME": str(home), "PATH": os.environ["PATH"]}
cfg = m._config(env)
result = {"autorepair": cfg.autorepair_enabled}
if case.get("mode") == "repair":
    skills = home / ".claude" / "skills"
    skill_dir = skills / case["name"]
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(case["content"], encoding="utf-8")
    marker = {"schema_version": 2, "manager": "ccc-node-skill-autosave", "ownership": "autosave-managed",
              "provider": "claude", "name": case["name"], "target_id": "t", "skill_sha256": case["old_sha"],
              "created_by": "ccc-node", "rollback_eligible": True, "provenance_revision": 1}
    (skill_dir / ".autosave-meta.json").write_text(json.dumps(marker), encoding="utf-8")
    try:
        if case["call"] == "frontmatter":
            m._repair_skill_frontmatter(skill_dir, case["name"])
            body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            result.update({"name_ok": f"name: {case['name']}" in body,
                           "desc_kept": "Validate the demo procedure" in body})
        elif case["call"] == "couplings":
            repaired_flag = m._autorepair_candidate(cfg, "claude", case["name"], "runtime_specific_claude")
            body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            result.update({"repaired_flag": repaired_flag,
                           "coupling_free": ("claude -p" not in body and "CLAUDE_MODEL" not in body)})
        else:
            sys.exit(3)
    except m.PromotionError as e:
        result.update({"error": e.code})
print(json.dumps(result))
PYDRIVER

ar_case() { # $1 = case json content, echoes driver output
  printf '%s' "$1" > "$TMP/ar-case.json"
  env "PATH=$BIN:$PATH" T_AUTOREPAIR="${T_AUTOREPAIR:-}" \
    python3 "$TMP/ar-driver.py" "$PROMOTER" "$TMP" "$TMP/ar-case.json"
}

# default off
T_AUTOREPAIR='' out="$(ar_case '{"mode":"config"}')"
ok "autorepair defaults to off" 'jq -e ".autorepair | not" >/dev/null <<<"$out"'

# state file enables
mkdir -p "$STATE" && printf 'true\n' > "$STATE/skill-promotion.autorepair" && chmod 600 "$STATE/skill-promotion.autorepair"
T_AUTOREPAIR='' out="$(ar_case '{"mode":"config"}')"
ok "autorepair state file enables the gate" 'jq -e ".autorepair == true" >/dev/null <<<"$out"'

# frontmatter repair: broken frontmatter rebuilt, name restored, marker re-stamped
cat > "$TMP/case-fm.json" <<'JSON'
{"mode":"repair","call":"frontmatter","name":"demo-skill","old_sha":"old","content":"---\nname: wrong-name\nclumsy line without colon\n---\n\n## When to Use\n\nValidate the demo procedure against a live system and record the evidence.\n"}
JSON
T_AUTOREPAIR=1 out="$(ar_case "$(cat "$TMP/case-fm.json")")"
ok "frontmatter autorepair rebuilds name and derives description" \
  'jq -e ".name_ok == true and .desc_kept == true" >/dev/null <<<"$out"'

# coupling repair: stub claude neutralizes runtime-specific text
cat > "$BIN/claude" <<'STUB'
#!/usr/bin/env bash
prompt="${*: -1}"
body="${prompt##*---}"
printf '%s\n' "$(printf '%s' "$body" | sed -E 's/claude -p/the coding agent CLI/g; s/CLAUDE_MODEL/the model env/g; s|\.claude/|the agent config dir|g')"
STUB
chmod +x "$BIN/claude"
cat > "$TMP/case-coupling.json" <<'JSON'
{"mode":"repair","call":"couplings","name":"demo-skill","old_sha":"old","content":"---\nname: demo-skill\ndescription: Run the model verify procedure against the live worker and record outputs.\n---\n\n## When to Use\n\nRun `claude -p` with CLAUDE_MODEL set and inspect the .claude/ config dir.\n"}
JSON
T_AUTOREPAIR=1 out="$(ar_case "$(cat "$TMP/case-coupling.json")")"
ok "runtime coupling repair neutralizes text and re-stamps marker" \
  'jq -e ".repaired_flag == true and .coupling_free == true" >/dev/null <<<"$out"'

# persisting coupling -> fail-closed error
cat > "$BIN/claude-stub-echo" <<'STUB'
#!/usr/bin/env bash
cat
STUB
chmod +x "$BIN/claude-stub-echo"
mkdir -p "$TMP/badbin"
cat > "$TMP/badbin/claude" <<'BADSTUB'
#!/usr/bin/env bash
for _ in 1 2 3; do printf '%s\n' "still says claude -p and CLAUDE_MODEL here"; done
BADSTUB
chmod +x "$TMP/badbin/claude"
out="$(PATH="$TMP/badbin:$(dirname "$(command -v python3)"):/usr/bin:/bin" T_AUTOREPAIR=1 python3 - "$PROMOTER" "$TMP" "$TMP/case-coupling.json" <<'PYINNER'
import importlib.util, json, os, pathlib, sys
promoter, tmp, case_path = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("promo_ar2", promoter)
m = importlib.util.module_from_spec(spec); sys.modules["promo_ar2"] = m; spec.loader.exec_module(m)
case = json.load(open(case_path))
home = pathlib.Path(tmp) / "ar-home2"
env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
cfg = m._config(env)
skills = home / ".claude" / "skills"
skill_dir = skills / case["name"]
skill_dir.mkdir(parents=True, exist_ok=True)
(skill_dir / "SKILL.md").write_text(case["content"], encoding="utf-8")
try:
    m._autorepair_llm(skill_dir, "claude", cfg.review_llm_cmd)
    print(json.dumps({"repaired": True}))
except m.PromotionError as e:
    print(json.dumps({"error": e.code}))
PYINNER
)"
ok "unrepaired couplings fail closed" 'jq -e ".error == \"runtime_specific_claude\"" >/dev/null <<<"$out"'

# ─── R2 revise rounds (#1357) + CCC_SKILL_REVIEW_LLM_CMD ───────────────────

# Config: revise tri-state and the bounded review LLM command contract.
out="$(env "${base_env[@]}" CCC_SKILL_PROMOTION_REVISE=maybe python3 "$PROMOTER" status)"; rc=$?
ok "invalid revise flag fails closed" \
  '[ "$rc" = 2 ] && jq -e ".code == \"revise_invalid\"" >/dev/null <<<"$out"'
out="$(env "${base_env[@]}" CCC_SKILL_REVIEW_LLM_CMD='   ' python3 "$PROMOTER" status)"; rc=$?
ok "empty review LLM command fails closed" \
  '[ "$rc" = 2 ] && jq -e ".code == \"review_llm_cmd_invalid\"" >/dev/null <<<"$out"'
cat > "$TMP/llm-driver.py" <<'PY'
import importlib.util, json, pathlib, sys
promoter, tmp, llm_cmd = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("promo_llm", promoter)
m = importlib.util.module_from_spec(spec); sys.modules["promo_llm"] = m; spec.loader.exec_module(m)
home = pathlib.Path(tmp) / "llm-home"
env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "CCC_SKILL_REVIEW_LLM_CMD": llm_cmd}
cfg = m._config(env)
skill_dir = home / ".claude" / "skills" / "llm-cmd-skill"
skill_dir.mkdir(parents=True, exist_ok=True)
(skill_dir / "SKILL.md").write_text(
    "---\nname: llm-cmd-skill\ndescription: Run the model verify procedure against the live worker and record outputs.\n"
    "---\n\n## Steps\n\nRun `claude -p` and check CLAUDE_MODEL before continuing.\n", encoding="utf-8")
try:
    m._autorepair_llm(skill_dir, "claude", cfg.review_llm_cmd)
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    print(json.dumps({"cmd": list(cfg.review_llm_cmd), "repaired": "claude -p" not in body}))
except m.PromotionError as e:
    print(json.dumps({"cmd": list(cfg.review_llm_cmd), "error": e.code}))
PY
cat > "$TMP/neutralize" <<'STUB'
#!/usr/bin/env bash
prompt="${*: -1}"
body="${prompt##*---}"
printf '%s\n' "$(printf '%s' "$body" | sed -E 's/claude -p/the coding agent CLI/g; s/CLAUDE_MODEL/the model env/g')"
STUB
chmod +x "$TMP/neutralize"
out="$(env "PATH=$BIN:$PATH" python3 "$TMP/llm-driver.py" "$PROMOTER" "$TMP" "$TMP/neutralize")"
ok "custom review LLM command repairs the candidate" \
  'jq -e ".repaired == true" >/dev/null <<<"$out" && jq -r ".cmd[0]" <<<"$out" | grep -q neutralize'

# ─── R2 end-to-end: dispatch → verdict → revise → republish (hermetic) ──────

R2TMP="$TMP/r2"
NEXUS="$R2TMP/a2a-nexus"
CURL_STATE="$R2TMP/curl-state"
R2_GH_STATE="$R2TMP/gh-state"
mkdir -p "$NEXUS/docs" "$NEXUS/scripts" "$CURL_STATE" "$R2_GH_STATE"
cat > "$NEXUS/docs/skills-intake-review.md" <<'DOC'
# intake review

## Worker procedure

Read the packet in full and apply the rubric in order, recording one finding
per failed check. Reviewers never modify the packet, never re-dispatch on a
failed verdict, and never see the edge secret. Severity floors apply: any
blocker forces reject and any major forces at least revise. Emit the verdict
JSON only, bound to the exact skill name, source tree hash, and head prefix.

## Receipt projection

On PASS the publisher composes the receipt from the broker result.
DOC
cat > "$NEXUS/docs/skills-intake-revise.md" <<'DOC'
# intake revise

## Worker procedure

Read the findings and the candidate copy in full; touch nothing else. The
revision is a holistic edit: regenerate the full revised set rather than
appending tail rules, keep the frontmatter name, and keep the description an
honest what-and-when router. Address every major and blocker finding, note
anything unresolved in the change summary, and never weaken safety or add
credentials, node facts, or runtime-specific couplings. If the candidate is a
single-incident checklist that cannot be generalized, return a drop
recommendation with the reason instead of a cosmetic rewrite. Emit the result
JSON only, bound to the exact skill name and source tree hash from the packet.

## Result schema

See the reviseResultSchema field embedded in the packet.
DOC
: > "$NEXUS/scripts/a2a-dispatch-round.mjs"
printf '{"brokerId":"seoseo"}\n' > "$CURL_STATE/health.json"
printf '{"items":[{"nodeId":"testnode"},{"nodeId":"reviewer1"}]}\n' > "$CURL_STATE/workers.json"
printf '{"status":"queued"}\n' > "$CURL_STATE/review-task.json"
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
set -eu
url=""
for argument in "$@"; do case "$argument" in http*) url="$argument";; esac; done
state="$CURL_TEST_STATE"
case "$url" in
  */health) cat "$state/health.json" ;;
  */workers) cat "$state/workers.json" ;;
  *skills_intake_revise*)
    pr="$(printf '%s' "$url" | sed -E 's|.*-pr([0-9]+)-.*|\1|')"
    file="$state/revise-task-pr$pr.json"
    [ -f "$file" ] || printf '{"status":"queued"}\n' > "$file"
    cat "$file" ;;
  */tasks/*) cat "$state/review-task.json" ;;
  *) printf '{}\n' ;;
esac
STUB
chmod +x "$BIN/curl"
cat > "$BIN/node" <<'STUB'
#!/usr/bin/env bash
set -eu
manifest=""
previous=""
for argument in "$@"; do
  [ "$previous" = "--manifest" ] && manifest="$argument"
  previous="$argument"
done
lane_id="$(jq -r '.lanes[0].id' "$manifest")"
printf '{"results":[{"taskId":"%s-broker-1"}]}\n' "$lane_id"
STUB
chmod +x "$BIN/node"
# Extend the gh stub: pr comments, api reads against the bare remote.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
set -eu
state="$GH_TEST_STATE"
remote_git="$GH_TEST_REMOTE"
mkdir -p "$state"
printf '%s\n' "$*" >> "$state/calls"
case "${1:-} ${2:-}" in
  "auth status") exit 0 ;;
  "repo view")
    if [ "${GH_TEST_PRIVATE:-true}" = "true" ]; then
      printf '{"isPrivate":true,"visibility":"PRIVATE"}\n'
    else
      printf '{"isPrivate":false,"visibility":"PUBLIC"}\n'
    fi ;;
  "pr list")
    head=""
    previous=""
    for argument in "$@"; do
      [ "$previous" = "--head" ] && head="$argument"
      previous="$argument"
    done
    key="$(printf '%s' "$head" | sha256sum | awk '{print $1}')"
    if [ -f "$state/created-$key" ]; then
      printf '[{"url":"https://github.com/test/repo/pull/1","state":"OPEN","isDraft":true}]\n'
    else
      printf '[]\n'
    fi ;;
  "pr create")
    head=""
    previous=""
    for argument in "$@"; do
      [ "$previous" = "--head" ] && head="$argument"
      previous="$argument"
    done
    key="$(printf '%s' "$head" | sha256sum | awk '{print $1}')"
    printf '%s\n' "$*" >> "$state/create.args"
    : > "$state/created-$key"
    number="$(printf '%s\n' "$state"/created-* 2>/dev/null | wc -l)"
    printf 'https://github.com/test/repo/pull/%s\n' "$number" ;;
  "pr comment")
    body=""
    previous=""
    for argument in "$@"; do
      [ "$previous" = "--body" ] && body="$argument"
      previous="$argument"
    done
    printf '%s\n' "$body" >> "$state/comments" ;;
  "api "*)
    url="$2"
    jq_expr=""
    [ "${3:-}" = "--jq" ] && jq_expr="$4"
    case "$url" in
      */check-runs) printf 'completed/success\n' ;;
      */commits/*)
        ref="${url#*/commits/}"
        git --git-dir="$remote_git" rev-parse "$ref" ;;
      *git/trees/*)
        ref="$(printf '%s' "$url" | sed -E 's|.*/git/trees/([^/?]+).*|\1|')"
        payload="$(git --git-dir="$remote_git" ls-tree -r "$ref" \
          | jq -Rs 'split("\n") | map(select(length > 0) | split("\t")
                    | {mode: (.[0] | split(" ")[0]), type: "blob", path: .[1]})')"
        if [ -n "$jq_expr" ]; then
          printf '{"tree": %s}' "$payload" | jq "$jq_expr"
        else
          printf '{"tree": %s}\n' "$payload"
        fi ;;
      *contents/*)
        path="${url#*contents/}"; path="${path%%\?*}"
        ref="${url##*ref=}"
        content="$(git --git-dir="$remote_git" show "$ref:$path" | base64 -w0)"
        printf '{"content": "%s"}\n' "$content" ;;
      *) exit 9 ;;
    esac ;;
  *) exit 9 ;;
esac
STUB
chmod +x "$BIN/gh"
# Keyring on the remote main: reviewer1 reviews, testnode revises.
KEYRING="$R2TMP/keyring"
git clone -q "$REMOTE" "$KEYRING"
mkdir -p "$KEYRING/refs"
jq -n '{keys:{"worker:reviewer1:g1:v1":{node:"reviewer1"},"worker:testnode:g1:v1":{node:"testnode"}}}' \
  > "$KEYRING/refs/a2a-public-keyring.json"
git -C "$KEYRING" add refs
git -C "$KEYRING" -c user.name=test -c user.email=test@example.invalid commit -qm keyring
git -C "$KEYRING" push -q origin main
r2_env=(
  "${publish_env[@]}"
  "CCC_SKILL_PROMOTION_DISPATCH=true"
  "CCC_SKILL_PROMOTION_REVISE=true"
  "CCC_SKILL_PROMOTION_A2A_NEXUS_DIR=$NEXUS"
  "CCC_SKILL_PROMOTION_BROKER_URL=http://broker.test"
  "A2A_EDGE_SECRET=test-secret"
  "CURL_TEST_STATE=$CURL_STATE"
  "GH_TEST_REMOTE=$REMOTE"
  "GH_TEST_STATE=$R2_GH_STATE"
)
ledger="$STATE/skill-promotion/ledger.jsonl"

write_verdict() { # $1 head, $2 verdict
  python3 - "$CURL_STATE/review-task.json" "$1" "${2:-revise}" <<'PY'
import json, sys
path, head, verdict = sys.argv[1], sys.argv[2], sys.argv[3]
task = {"status": "succeeded", "result": {"output": {
    "verdict": verdict,
    "findings": [{"severity": "major", "area": "utility",
                  "note": "Single-incident checklist; generalize before promotion."}],
    "evidence": [{"kind": "grep", "detail": "pins one incident"}],
    "model": "test-model", "reviewer_node": "reviewer1",
    "head_sha": head, "rubric_version": "2026-08-28.2",
}}}
json.dump(task, open(path, "w"))
PY
}
write_revised() { # $1 source tree, $2 extra step line, $3 intake pr number
  python3 - "$CURL_STATE/revise-task-pr${3:-1}.json" "$1" "${2:-4}" <<'PY'
import json, sys
path, tree, marker = sys.argv[1], sys.argv[2], sys.argv[3]
skill = ("---\nname: r2-skill\ndescription: Capture a reusable and safely shareable release verification procedure.\n"
         "---\n\n# Procedure\n\n1. Inspect the release state.\n2. Run the bounded verification.\n"
         f"3. Record the result.\n{marker}. Generalize the evidence for any release, not one incident.\n")
task = {"status": "succeeded", "result": {"output": {
    "outcome": "revised", "skillName": "r2-skill", "sourceTreeSha256": tree,
    "skillFiles": [{"path": "SKILL.md", "content": skill}],
    "changeSummary": "Generalized per the major utility finding.", "model": "test-model",
    "reviser_node": "testnode"}}}
json.dump(task, open(path, "w"))
PY
}
latest_tree() { jq -r 'select(.kind=="a2a-revise-result" and .status=="republished") | .new_tree_sha256' "$ledger" | tail -1; }
branch_of() { printf 'skill-intake/testnode/r2-skill-claude-%s' "${1:0:12}"; }

# Phase 1: PR opens, review dispatches, revise poll pends on the queued task.
write_skill r2-skill ""
write_status r2-skill
env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true "PATH=$BIN:$PATH" python3 "$PROMOTER" run >/dev/null
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
branches="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname:short)' refs/heads | grep '^skill-intake/testnode/r2-skill' | sort)"
branch1="$(head -1 <<<"$branches")"
head1="$(git --git-dir="$REMOTE" rev-parse "$branch1")"
tree1="$(jq -r '.published[0].tree_sha256' <<<"$out")"
ok "R2 p1: PR opened, review dispatched, queued verdict pends" \
  '[ "$rc" = 0 ] && jq -e ".published[0].outcome == \"pr-opened\" and .revise.verdicts[0].outcome == \"verdict-pending\" and (.revise.results | length) == 0" >/dev/null <<<"$out"'
ok "R2 p1: review dispatch ledger row carries lineage fields" \
  'jq -e "select(.kind==\"a2a-dispatch\") | .node==\"testnode\" and .provider==\"claude\" and .name==\"r2-skill\" and .reviewer_node==\"reviewer1\"" >/dev/null "$ledger"'

# Autonomy dry-run only polls — no verdict consumption, no comments.
out="$(env "${r2_env[@]}" CCC_AUTONOMY=dry-run python3 "$PROMOTER" collect)"; rc=$?
ok "R2: autonomy dry-run polls verdicts without consuming" \
  '[ "$rc" = 0 ] && jq -e ".mode == \"collect-dry-run\" and .revise.verdicts[0].outcome == \"would-poll-verdict\"" >/dev/null <<<"$out"'

# Phase 2: revise verdict → findings comment + revision dispatch to the author.
write_verdict "$head1" revise
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "R2 p2: revise verdict triggers round-1 revision dispatch" \
  '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].verdict == \"revise\" and .revise.verdicts[0].revise.outcome == \"revise-dispatched\" and .revise.verdicts[0].revise.round == 1" >/dev/null <<<"$out"'
ok "R2 p2: revise dispatch row binds lineage, round, and revise lane" \
  'jq -e "select(.kind==\"a2a-revise-dispatch\") | .node==\"testnode\" and .name==\"r2-skill\" and .round==1 and .reviser_node==\"testnode\" and (.task_id|startswith(\"skills_intake_revise-pr1\"))" >/dev/null "$ledger"'
ok "R2 p2: verdict and findings become visible on the intake PR" \
  'grep -q "auto-revision round 1/2" "$R2_GH_STATE/comments" && grep -q "major/utility" "$R2_GH_STATE/comments"'

# Phase 3: reviser returns a valid revision → full re-gate → fresh intake PR.
write_revised "$tree1" 4
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "R2 p3: revised tree republished through the full gate path" \
  '[ "$rc" = 0 ] && jq -e ".revise.results[0].outcome == \"republished\"" >/dev/null <<<"$out"'
ok "R2 p3: fresh intake PR opened and independent re-review dispatched" \
  'jq -e "select(.kind==\"a2a-revise-result\" and .status==\"republished\") | .new_pr_url | endswith(\"pull/2\")" >/dev/null "$ledger" && [ "$(jq -s "[.[] | select(.kind==\"a2a-dispatch\")] | length" "$ledger")" -ge 2 ]'
ok "R2 p3: superseded PR keeps its human lane (no auto-close)" \
  'grep -q "superseded and left open" "$R2_GH_STATE/comments"'
tree2="$(latest_tree)"
head2="$(git --git-dir="$REMOTE" rev-parse "$(branch_of "$tree2")")"

# Phase 4: second revise verdict on the fresh PR → round 2.
write_verdict "$head2" revise
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "R2 p4: second round dispatches while under the lineage cap" \
  '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].revise.outcome == \"revise-dispatched\" and .revise.verdicts[0].revise.round == 2" >/dev/null <<<"$out"'

# Phase 5: second revision → second republish.
write_revised "$tree2" 5 2
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "R2 p5: round-2 revision republishes a third tree" \
  '[ "$rc" = 0 ] && jq -e ".revise.results[0].outcome == \"republished\"" >/dev/null <<<"$out"'
tree3="$(latest_tree)"
head3="$(git --git-dir="$REMOTE" rev-parse "$(branch_of "$tree3")")"

# Phase 6: third revise verdict exceeds the cap → handoff, no auto-close.
write_verdict "$head3" revise
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "R2 p6: round cap reached — handoff to human, no further dispatch" \
  '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].verdict == \"revise\" and .revise.verdicts[0].revise.outcome == \"revise-handoff\"" >/dev/null <<<"$out"'
ok "R2 p6: exactly two revision rounds ran for the lineage" \
  '[ "$(jq -s "[.[] | select(.kind==\"a2a-revise-dispatch\")] | length" "$ledger")" = 2 ] && grep -q "Revise round limit reached" "$R2_GH_STATE/comments"'

# Scenario: drop recommendation is recorded for the human sweep, not executed.
printf '{"status":"queued"}\n' > "$CURL_STATE/review-task.json"
write_skill r2-drop ""
write_status r2-drop
env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true "PATH=$BIN:$PATH" python3 "$PROMOTER" run >/dev/null
drop_out1="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
out="$drop_out1"
branch_drop="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname:short)' refs/heads | grep 'r2-drop' | head -1)"
head_drop="$(git --git-dir="$REMOTE" rev-parse "$branch_drop")"
manifest_drop="$(git --git-dir="$REMOTE" ls-tree -r --name-only "$branch_drop" | grep 'manifest.json' | head -1)"
tree_drop="$(git --git-dir="$REMOTE" show "$branch_drop:$manifest_drop" | jq -r .tree_sha256)"
write_verdict "$head_drop" revise
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
pr_drop="$(jq -r '.published[0].url' <<<"$drop_out1" | grep -o '[0-9]*$')"
python3 - "$CURL_STATE/revise-task-pr$pr_drop.json" "$tree_drop" <<'PY'
import json, sys
path, tree = sys.argv[1], sys.argv[2]
json.dump({"status": "succeeded", "result": {"output": {
    "outcome": "drop_recommendation", "skillName": "r2-drop", "sourceTreeSha256": tree,
    "dropRecommendation": {"reason": "Pins one outage; no general procedure survives."},
    "model": "test-model", "reviser_node": "testnode"}}}, open(path, "w"))
PY
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "drop recommendation is recorded for the human sweep"   '[ "$rc" = 0 ] && jq -e ".revise.results[0].outcome == \"drop-recommended\"" >/dev/null <<<"$out" && jq -e "select(.kind==\"a2a-revise-result\" and .status==\"drop-recommended\") | .reason | startswith(\"Pins one outage\")" >/dev/null "$ledger"'
ok "drop recommendation never closes or republishes"   'grep -q "Drop recommendation (auto-revision gate)" "$R2_GH_STATE/comments" && [ "$(jq -s "[.[] | select(.name==\"r2-drop\" and .kind==\"a2a-revise-result\" and .status==\"republished\")] | length" "$ledger")" = 0 ]'

# Scenario: a malformed verdict is a handler failure — consumed once, no PR
# comment, no revision dispatch.
printf '{"status":"queued"}\n' > "$CURL_STATE/review-task.json"
write_skill r2-mal ""
write_status r2-mal
env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true "PATH=$BIN:$PATH" python3 "$PROMOTER" run >/dev/null
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
branch_mal="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname:short)' refs/heads | grep 'r2-mal' | head -1)"
head_mal="$(git --git-dir="$REMOTE" rev-parse "$branch_mal")"
python3 - "$CURL_STATE/review-task.json" "$head_mal" <<'PY'
import json, sys
path, head = sys.argv[1], sys.argv[2]
json.dump({"status": "succeeded", "result": {"output": {
    "verdict": "excellent", "head_sha": head}}}, open(path, "w"))
PY
before_comments="$(wc -l < "$R2_GH_STATE/comments" 2>/dev/null || echo 0)"
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "malformed verdict consumed as a handler failure"   '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].outcome == \"verdict-malformed\"" >/dev/null <<<"$out" && jq -e "select(.kind==\"a2a-verdict\" and .status==\"malformed\")" >/dev/null "$ledger"'
ok "malformed verdict triggers no comment and no revision"   '[ "$(wc -l < "$R2_GH_STATE/comments")" = "$before_comments" ] && [ "$(jq -s "[.[] | select(.name==\"r2-mal\" and .kind==\"a2a-revise-dispatch\")] | length" "$ledger")" = 0 ]'

# Scenario: per-node daily cap bounds same-day revision cost.
printf '{"status":"queued"}\n' > "$CURL_STATE/review-task.json"
write_skill r2-cap ""
write_status r2-cap
env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true "PATH=$BIN:$PATH" python3 "$PROMOTER" run >/dev/null
cap_env=("${r2_env[@]}" "CCC_SKILL_PROMOTION_REVISE_DAILY_CAP=1")
cap_out0="$(env "${cap_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
out="$cap_out0"
branch_cap="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname:short)' refs/heads | grep 'r2-cap' | head -1)"
head_cap="$(git --git-dir="$REMOTE" rev-parse "$branch_cap")"
manifest_cap="$(git --git-dir="$REMOTE" ls-tree -r --name-only "$branch_cap" | grep 'manifest.json' | head -1)"
tree_cap="$(git --git-dir="$REMOTE" show "$branch_cap:$manifest_cap" | jq -r .tree_sha256)"
write_verdict "$head_cap" revise
cap_out1="$(env "${cap_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
out="$cap_out1"
ok "daily cap admits the first revision dispatch"   '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].revise.outcome == \"revise-dispatched\"" >/dev/null <<<"$out"'
pr_cap="$(jq -r '.published[0].url' <<<"$cap_out0" | grep -o '[0-9]*$')"
python3 - "$CURL_STATE/revise-task-pr$pr_cap.json" "$tree_cap" <<'PY'
import json, sys
path, tree = sys.argv[1], sys.argv[2]
skill = ("---\nname: r2-cap\ndescription: Capture a reusable and safely shareable release verification procedure.\n"
         "---\n\n# Procedure\n\n1. Inspect the release state.\n2. Run the bounded verification.\n"
         "3. Record the result.\n4. Generalize the evidence for any release.\n")
json.dump({"status": "succeeded", "result": {"output": {
    "outcome": "revised", "skillName": "r2-cap", "sourceTreeSha256": tree,
    "skillFiles": [{"path": "SKILL.md", "content": skill}],
    "changeSummary": "Generalized.", "model": "test-model", "reviser_node": "testnode"}}},
    open(path, "w"))
PY
out="$(env "${cap_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
branch_cap2="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname:short)' refs/heads | grep 'r2-cap' | sort | tail -1)"
head_cap2="$(git --git-dir="$REMOTE" rev-parse "$branch_cap2")"
write_verdict "$head_cap2" revise
out="$(env "${cap_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "daily cap blocks the second same-day revision dispatch"   '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].revise.outcome == \"revise-skipped\" and .revise.verdicts[0].revise.code == \"revise_daily_cap\"" >/dev/null <<<"$out"'

# Scenario (#1370): a revise verdict whose R2 dispatch SKIPS must still surface
# the verdict+findings on the PR — exactly once, with a human-readable reason.
# Live gap (2026-08-31, fleet-skills#79): revise_author_offline consumed the
# verdict and left the PR with zero trace of the review.
printf '{"status":"queued"}\n' > "$CURL_STATE/review-task.json"
write_skill r2-off ""
write_status r2-off
env "${base_env[@]}" CCC_SKILL_PROMOTION_ENABLED=true "PATH=$BIN:$PATH" python3 "$PROMOTER" run >/dev/null
off_out0="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
out="$off_out0"
branch_off="$(git --git-dir="$REMOTE" for-each-ref --format='%(refname:short)' refs/heads | grep 'r2-off' | head -1)"
head_off="$(git --git-dir="$REMOTE" rev-parse "$branch_off")"
# Author node (testnode) drops off the broker's online-worker list:
printf '{"items":[{"nodeId":"reviewer1"}]}\n' > "$CURL_STATE/workers.json"
write_verdict "$head_off" revise
skip_rows() { jq -s "[.[] | select(.kind==\"a2a-revise-comment\" and .marker==\"revise-verdict:revise_author_offline\")] | length" "$ledger"; }
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "#1370: offline-author revise skip is recorded as revise-skipped" \
  '[ "$rc" = 0 ] && jq -e ".revise.verdicts[0].verdict == \"revise\" and .revise.verdicts[0].revise.outcome == \"revise-skipped\" and .revise.verdicts[0].revise.code == \"revise_author_offline\"" >/dev/null <<<"$out"'
ok "#1370: skipped verdict still posts the verdict comment once (ledger-marked)" \
  '[ "$(skip_rows)" = 1 ] && [ "$(grep -c "revise_author_offline" "$R2_GH_STATE/comments")" = 1 ]'
ok "#1370: the skip comment carries a human-readable reason and the findings" \
  'grep -q "stay visible for human follow-up" "$R2_GH_STATE/comments" && grep -q "Single-incident checklist" "$R2_GH_STATE/comments"'
out="$(env "${r2_env[@]}" python3 "$PROMOTER" collect)"; rc=$?
ok "#1370: repeated collect does not duplicate the skip comment" \
  '[ "$(skip_rows)" = 1 ] && [ "$(grep -c "revise_author_offline" "$R2_GH_STATE/comments")" = 1 ]'
printf '{"items":[{"nodeId":"testnode"},{"nodeId":"reviewer1"}]}\n' > "$CURL_STATE/workers.json"

# ─── R3 drop-report: read-only human sweep over drop recommendations (#1363)

DROP_HOME="$TMP/drop-home"
DROP_STATE="$DROP_HOME/.claude/state"
DROP_DIR="$DROP_STATE/skill-promotion"
DROP_LEDGER="$DROP_DIR/ledger.jsonl"
mkdir -p "$DROP_DIR"
chmod 700 "$DROP_HOME" "$DROP_HOME/.claude" "$DROP_STATE" "$DROP_DIR"
touch "$DROP_LEDGER" && chmod 600 "$DROP_LEDGER"
drop_env=(
  "HOME=$DROP_HOME" "CCC_CLAUDE_DIR=$DROP_HOME/.claude" "CCC_STATE_DIR=$DROP_STATE"
  "CCC_SKILL_PROMOTION_ENABLED=true" "CCC_NODE=testnode"
  "CCC_SKILL_PROMOTION_REPO=test/repo" "CCC_SKILL_PROMOTION_PROVIDERS=claude"
  "CCC_SKILL_PROMOTION_OWNERSHIP_TOOL=$OWNERSHIP" "PROMOTION_TEST_STATUS=$STATUS_JSON"
)
drop_row() { jq -cn --argjson row "$1" '$row' >> "$DROP_LEDGER"; }

out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "drop-report on an empty ledger is ok and empty" \
  '[ "$rc" = 0 ] && jq -e ".ok and .summary.pending_items == 0 and (.pending | length) == 0" >/dev/null <<<"$out"'

drop_row '{"ts":"2026-08-28T10:00:00Z","kind":"a2a-revise-dispatch","task_id":"drop-task-a",
  "pr":"101","pr_url":"https://github.com/test/repo/pull/101","node":"nodea",
  "provider":"claude","name":"skilla","round":1}'
drop_row '{"ts":"2026-08-28T12:00:00Z","kind":"a2a-revise-result","task_id":"drop-task-a",
  "status":"drop-recommended","node":"nodea","name":"skilla","pr":"101",
  "reason":"Pins one outage."}'
drop_row '{"ts":"2026-08-29T09:00:00Z","kind":"a2a-revise-dispatch","task_id":"drop-task-b",
  "pr":"102","pr_url":"https://github.com/test/repo/pull/102","node":"nodeb",
  "provider":"codex","name":"skillb","round":2}'
drop_row '{"ts":"2026-08-29T11:00:00Z","kind":"a2a-revise-result","task_id":"drop-task-b",
  "status":"drop-recommended","node":"nodeb","name":"skillb","pr":"102",
  "reason":"Single-incident only."}'
drop_row '{"ts":"2026-08-29T12:00:00Z","kind":"a2a-verdict","task_id":"other","status":"ok"}'
ledger_lines_before="$(grep -c . "$DROP_LEDGER")"

out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "drop-report joins dispatch rows and reports both lineages" \
  '[ "$rc" = 0 ] && jq -e ".summary.pending_items == 2 and .summary.pending_lineages == 2
    and .summary.new_items == 2 and .summary.nodes.nodea == 1 and .summary.nodes.nodeb == 1
    and .sweep_recorded" >/dev/null <<<"$out"'
ok "drop-report items carry the human-sweep fields" \
  'jq -e ".pending[0].node == \"nodea\" and .pending[0].name == \"skilla\"
    and .pending[0].provider == \"claude\" and .pending[0].round == 1
    and .pending[0].pr_url == \"https://github.com/test/repo/pull/101\"
    and .pending[0].reason == \"Pins one outage.\"
    and .pending[0].first_recorded_at == \"2026-08-28T12:00:00Z\"" >/dev/null <<<"$out"'
ok "drop-report never writes to the ledger" \
  '[ "$(grep -c . "$DROP_LEDGER")" = "$ledger_lines_before" ] && [ "$(jq -s "[.[] | select(.kind | startswith(\"drop-report\"))] | length" "$DROP_LEDGER")" = 0 ]'

out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "second sweep reports the same items as not new" \
  '[ "$rc" = 0 ] && jq -e ".summary.pending_items == 2 and .summary.new_items == 0" >/dev/null <<<"$out"'

out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report --ack drop-task-a)"; rc=$?
ok "ack marks one recommendation processed" \
  '[ "$rc" = 0 ] && jq -e ".ack[0].outcome == \"acked\" and .summary.pending_items == 1
    and .pending[0].task_id == \"drop-task-b\"" >/dev/null <<<"$out"'
out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report --ack drop-task-a)"; rc=$?
ok "re-ack is idempotent" \
  '[ "$rc" = 0 ] && jq -e ".ack[0].outcome == \"already-acked\" and .summary.pending_items == 1" >/dev/null <<<"$out"'

out="$(env "${drop_env[@]}" CCC_AUTONOMY=kill python3 "$PROMOTER" drop-report)"; rc=$?
ok "drop-report stays viewable under autonomy kill" \
  '[ "$rc" = 0 ] && jq -e ".ok and .summary.pending_items == 1" >/dev/null <<<"$out"'

ln -sf /nonexistent "$DROP_DIR/drop-report.acked.jsonl"
out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "drop-report fails closed on an unsafe ack state file" \
  '[ "$rc" = 2 ] && jq -e ".code == \"drop_state_unsafe\"" >/dev/null <<<"$out"'
rm -f "$DROP_DIR/drop-report.acked.jsonl"

out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report --ack '../escape')"; rc=$?
ok "drop-report rejects unsafe ack ids" \
  '[ "$rc" = 2 ] && jq -e ".code == \"drop_ack_invalid\"" >/dev/null <<<"$out"'

# #1477: ledger.jsonl is read whole; an oversized ledger (> 8 MiB) fails closed
# with ledger_too_large on every reader instead of stalling the run.
cp "$DROP_LEDGER" "$TMP/drop-ledger.bak"
truncate -s $((8 * 1024 * 1024 + 1)) "$DROP_LEDGER"
out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "#1477: drop-report fails closed on an oversized ledger" \
  '[ "$rc" = 2 ] && jq -e ".ok == false and .code == \"ledger_too_large\"" >/dev/null <<<"$out"'
out="$(env "${drop_env[@]}" CCC_SKILL_PROMOTION_PUBLISHER=true CCC_SKILL_PROMOTION_REMOTE="$REMOTE" \
  CCC_SKILL_PROMOTION_REVISE=true GH_TEST_STATE="$TMP/oversize-gh-state" GH_TEST_PRIVATE=true \
  GH_TEST_REMOTE="$REMOTE" PATH="$BIN:$PATH" python3 "$PROMOTER" collect)"; rc=$?
ok "#1477: collect fails closed on an oversized ledger before polling verdicts" \
  '[ "$rc" = 2 ] && jq -e ".ok == false and .code == \"ledger_too_large\"" >/dev/null <<<"$out"'
truncate -s $((8 * 1024 * 1024)) "$DROP_LEDGER"
out="$(env "${drop_env[@]}" python3 "$PROMOTER" drop-report)"; rc=$?
ok "#1477: a ledger exactly at the cap is still readable" \
  '[ "$rc" = 0 ] && jq -e ".ok and .mode == \"drop-report\"" >/dev/null <<<"$out"'
cp "$TMP/drop-ledger.bak" "$DROP_LEDGER"

# ─── negative-verdict binding (a2a-nexus#2016) ───────────────────────────────
# Failed review tasks carry the negative verdict as negativeVerdictEvidence;
# the publisher must bind it (visibility + bounded revision), while a bare
# crash with no evidence stays malformed.

NVE_FIXTURE="$TMP/nve-fixture.json"
cat > "$NVE_FIXTURE" <<'FIXTURE'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("promo_nve", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["promo_nve"] = m
spec.loader.exec_module(m)
head = "a" * 64
succeeded = {"status": "succeeded",
             "result": {"output": {"verdict": "approve", "head_sha": head, "findings": []}}}
failed_nve = {"status": "failed",
              "error": {"code": "review_verdict_failed"},
              "negativeVerdictEvidence": {"reviewerNodeId": "rn", "verdict": "fail",
                "result": {"output": {"verdict": "revise", "head_sha": head,
                  "findings": [{"severity": "major", "area": "claims", "note": "x"}]}}}}
bare_crash = {"status": "failed", "error": {"code": "handler_exit_nonzero"}}
ok1 = m._verdict_from_task(succeeded, head)
ok2 = m._verdict_from_task(failed_nve, head)
ok3 = m._verdict_from_task(bare_crash, head)
assert ok1 == ("approve", []), ok1
verdict, findings = ok2
assert verdict == "revise" and len(findings) == 1, ok2
assert ok3 is None, ok3
print("NVE-BINDING-OK")
FIXTURE
python3 "$NVE_FIXTURE" "$PROMOTER" >/dev/null 2>&1; rc=$?
ok "publisher binds negative verdict evidence from failed review tasks" '[ "$rc" = 0 ]'

# ---- #2024: remote broker mirroring — parser + reviewer fallthrough + task routing ----
RB_STATE="$TMP/rb-state"
mkdir -p "$RB_STATE"
RB_JSON='[{"name":"t2","ssh_host":"gwakga-stub","broker_url":"http://127.0.0.1:8787","nexus_dir":"/remote/nexus","secret_cmd":"cat /remote/edge-secret"}]'

cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
set -eu
for argument in "$@"; do
  case "$argument" in
    *a2a-public-keyring*)
      printf '{"content":"%s"}' "$(printf '{"keys":{"worker:authorx:g1:v1":{},"worker:t2reviewer:g1:v1":{}}}' | base64 -w0)"
      exit 0
      ;;
  esac
done
exit 1
STUB
chmod +x "$BIN/gh"

cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$RB_STATE/curl-calls"
case " $* " in
  *"/health"*) printf '{"brokerId":"primary-broker"}' ;;
  *"/workers"*) printf '{"items":[{"nodeId":"authorx","status":"online"}]}' ;;
  *) printf '{}' ;;
esac
STUB
chmod +x "$BIN/curl"

cat > "$BIN/ssh" <<'STUB'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$RB_STATE/ssh-calls"
case " $* " in
  *"/workers"*) printf '{"items":[{"nodeId":"t2reviewer","status":"online"},{"nodeId":"stale-one","status":"stale"}]}' ;;
  *"/health"*) printf '{"brokerId":"t2-broker"}' ;;
  *"/tasks/"*) printf '{"status":"running","id":"rv-x"}' ;;
  *) exit 8 ;;
esac
STUB
chmod +x "$BIN/ssh"

cat > "$BIN/scp" <<'STUB'
#!/usr/bin/env bash
set -eu
exit 0
STUB
chmod +x "$BIN/scp"

RB_FIXTURE="$TMP/rb-fixture.py"
cat > "$RB_FIXTURE" <<FIXTURE
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("csp_t2", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["csp_t2"] = m
spec.loader.exec_module(m)

assert m._parse_remote_brokers("") == ()
assert m._parse_remote_brokers("not-json") == ()
assert m._parse_remote_brokers("[]") == ()
assert m._parse_remote_brokers('[{"name":"bad"}]') == ()
assert m._parse_remote_brokers('[{"name":"primary","ssh_host":"x","broker_url":"http://x/","nexus_dir":"/n","secret_cmd":"c"}]') == ()
good = m._parse_remote_brokers('[{"name":"t2","ssh_host":"h","broker_url":"http://127.0.0.1:8787/","nexus_dir":"/n","secret_cmd":"c"}]')
assert len(good) == 1 and good[0]["broker_url"] == "http://127.0.0.1:8787"
print("RB-PARSE-OK")

config = m._config()
assert [rb["name"] for rb in config.remote_brokers] == ["t2"], config.remote_brokers

# Primary has only the author online -> fall through to the remote broker.
reviewer, rb = m._dispatch_target_worker(config, "authorx", "s3cret-value")
assert reviewer == "t2reviewer" and rb is not None and rb["name"] == "t2", (reviewer, rb)

# Primary eligible path is unchanged when the author lives on the remote.
reviewer2, rb2 = m._dispatch_target_worker(config, "t2reviewer", "s3cret-value")
assert reviewer2 == "authorx" and rb2 is None, (reviewer2, rb2)

# Row routing: 't2' row polls over ssh, legacy/primary row polls over curl.
task = m._broker_task_on(config, {"broker": "t2"}, "rv-x", "s3cret-value")
assert task.get("status") == "running", task
task2 = m._broker_task_on(config, {"kind": "a2a-dispatch"}, "rv-y", "s3cret-value")
assert isinstance(task2, dict)

# The secret VALUE never crosses to this node — only the remote command text.
ssh_calls = open(os.environ["RB_STATE"] + "/ssh-calls").read()
assert "s3cret-value" not in ssh_calls, "edge secret leaked into ssh argv"
assert "/remote/edge-secret" in ssh_calls
print("RB-ROUTING-OK")
FIXTURE
env "${base_env[@]}" CCC_SKILL_PROMOTION_REMOTE_BROKERS="$RB_JSON" PATH="$BIN:$PATH" RB_STATE="$RB_STATE" \
  python3 "$RB_FIXTURE" "$PROMOTER" > "$RB_STATE/out" 2>&1; rc=$?
ok "remote broker parse, reviewer fallthrough, and task routing (#2024)" \
  '[ "$rc" = 0 ] && grep -q "RB-PARSE-OK" "$RB_STATE/out" && grep -q "RB-ROUTING-OK" "$RB_STATE/out"'

# The mirrored reviewer actually lands in the dispatch manifest (no local
# dispatch here — the routing unit above covers broker selection).

# ─── #1370: _revise_outcome_note mapping (unit) ───────
NOTE_FIXTURE="$TMP/note-fixture.json"
cat > "$NOTE_FIXTURE" <<'FIXTURE'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("promo_note", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["promo_note"] = m
spec.loader.exec_module(m)
# A successful dispatch already posts the full verdict comment — no extra note.
assert m._revise_outcome_note({"outcome": "revise-dispatched"}) == "", "dispatched"
# Handoff surfaces the exhaustion and defers to human judgment.
note = m._revise_outcome_note({"outcome": "revise-handoff", "round": 2})
assert "exhausted" in note and "2 dispatched" in note and "human judgment" in note, note
# Known skip codes render the human text plus the raw code for greppability.
note = m._revise_outcome_note({"outcome": "revise-skipped", "code": "revise_author_offline"})
assert "not currently an online broker worker" in note and "(`revise_author_offline`)" in note, note
# Unknown codes still render, with the raw code; transport detail is appended.
note = m._revise_outcome_note({"outcome": "revise-skipped", "code": "revise_round_failed", "detail": "boom"})
assert "no automatic retry" in note and "boom" in note and "(`revise_round_failed`)" in note, note
note = m._revise_outcome_note({"outcome": "revise-skipped", "code": "revise_whatever"})
assert note == "revision dispatch skipped (`revise_whatever`)", note
print("NOTE-MAPPING-OK")
FIXTURE
python3 "$NOTE_FIXTURE" "$PROMOTER" >/dev/null 2>&1; rc=$?
ok "revise outcome notes map skip codes to human-readable text" '[ "$rc" = 0 ]'

# ---- #2027: publisher warns (never fails) on verdicts missing provenance ----
PROV_FIXTURE="$TMP/prov-fixture.py"
cat > "$PROV_FIXTURE" <<FIXTURE
import importlib.util, io, json, os, sys, contextlib
spec = importlib.util.spec_from_file_location("csp_prov", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["csp_prov"] = m
spec.loader.exec_module(m)

head = "c" * 40
base = {"taskId": "rv-prov", "verdict": "revise", "head_sha": head,
        "findings": [{"severity": "minor", "area": "quality", "note": "n"}]}

legacy = {"status": "succeeded", "result": {"output": dict(base)}}  # no provenance fields
modern = {"status": "succeeded", "result": {"output": dict(base, reviewer_node="rn", review_agent="pi", review_model="xai/grok-4.6")}}

err = io.StringIO()
with contextlib.redirect_stderr(err):
    out = m._verdict_from_task(legacy, head)
assert out is not None and out[0] == "revise", out
warned = err.getvalue()
assert "missing review provenance" in warned and "review_agent" in warned, warned

err2 = io.StringIO()
with contextlib.redirect_stderr(err2):
    out2 = m._verdict_from_task(modern, head)
assert out2 is not None and out2[0] == "revise", out2
assert "missing review provenance" not in err2.getvalue(), err2.getvalue()
print("PROV-WARN-OK")
FIXTURE
env "${base_env[@]}" python3 "$PROV_FIXTURE" "$PROMOTER" > "$TMP/prov-out" 2>&1; rc=$?
ok "publisher warns on legacy verdicts missing provenance, never fails (#2027)" \
  '[ "$rc" = 0 ] && grep -q "PROV-WARN-OK" "$TMP/prov-out"'

# --- #1394 defect 1: canon-lane revise targets get an explicit code -------
CANON_FIXTURE="$TMP/canon-fixture.py"
cat > "$CANON_FIXTURE" <<FIXTURE
import importlib.util, sys
spec = importlib.util.spec_from_file_location("csp_canon", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["csp_canon"] = m
spec.loader.exec_module(m)

head = "a" * 40
url = "https://github.com/test/repo/pull/52"

# Observed seoseo ledger rows (#1394): canon re-review lanes carry harness
# providers — R2 rows have the field, pre-R2 rows only the transport id.
r2_shared = {
    "transport_id": "ccc-node-shared-fleet-task-handoff-with-constraints-3834b22a8c84",
    "head_sha": head, "pr_url": url, "provider": "shared",
}
pre_r2_piri = {
    "transport_id": "ccc-node-piri-web-ad5ae95f22b1",
    "head_sha": head, "pr_url": "https://github.com/test/repo/pull/67",
}
pre_r2_bridge = {
    "transport_id": "ccc-node-bridge-setup-af322ef16783",
    "head_sha": head, "pr_url": "https://github.com/test/repo/pull/68",
}
canon_claude = {
    "transport_id": "ccc-node-claude-web-routing-1234567890ab",
    "head_sha": head, "pr_url": "https://github.com/test/repo/pull/70",
    "provider": "claude",
}
for label, row in (("r2-shared", r2_shared), ("pre-r2-piri", pre_r2_piri),
                   ("pre-r2-bridge", pre_r2_bridge), ("canon-claude", canon_claude)):
    got = m._revise_dispatch_target(row)
    assert got == "revise_canon_lane", (label, got)

# Regular author lanes still parse into the dispatch tuple.
tree = "0123456789ab"
regular = {
    "transport_id": f"testnode-claude-r2-skill-{tree}",
    "head_sha": head, "pr_url": "https://github.com/test/repo/pull/1",
    "provider": "claude",
}
got = m._revise_dispatch_target(regular)
assert got == ("testnode", "r2-skill", tree, "1", "claude", head, regular["pr_url"]), got

# Genuinely unknown providers stay revise_record_invalid.
bad = {
    "transport_id": "testnode-bogus-skill-0123456789ab",
    "head_sha": head, "pr_url": url, "provider": "bogus",
}
assert m._revise_dispatch_target(bad) == "revise_record_invalid", bad

# The skip note must exist so the #1370 verdict comment stays explanatory.
assert "revise_canon_lane" in m._REVISE_SKIP_NOTES
print("CANON-LANE-OK")
FIXTURE
env "${base_env[@]}" python3 "$CANON_FIXTURE" "$PROMOTER" > "$TMP/canon-out" 2>&1; rc=$?
ok "canon harness lanes return revise_canon_lane, regular lanes still parse (#1394)" \
  '[ "$rc" = 0 ] && grep -q "CANON-LANE-OK" "$TMP/canon-out"'

# --- #1394 defect 2: FIFO collect window drains backlogs -----------------
WINDOW_FIXTURE="$TMP/window-fixture.py"
cat > "$WINDOW_FIXTURE" <<FIXTURE
import importlib.util, json, os, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("csp_win", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["csp_win"] = m
spec.loader.exec_module(m)

# Isolated state: never touch the shared promotion state other tests use.
iso_state = Path(tempfile.mkdtemp()) / "skill-promotion"
iso_state.mkdir(parents=True, exist_ok=True)
env = dict(os.environ)
env["CCC_STATE_DIR"] = str(iso_state.parent)
state = iso_state
rows = []
for i in range(40):
    rows.append({
        "ts": f"2026-09-01T00:{i:02d}:00Z", "kind": "a2a-dispatch",
        "transport_id": f"testnode-claude-win-skill-{i:012x}",
        "head_sha": "d" * 40,
        "pr_url": "https://github.com/test/repo/pull/9",
        "dispatched_task": f"task-{i:02d}", "reviewer_node": "reviewer1",
        "node": "testnode", "provider": "claude",
        "name": f"win-skill-{i:012x}", "tree_sha256": "e" * 64,
        "branch": "skill-intake/testnode/win-skill",
    })
with open(state / "ledger.jsonl", "w", encoding="utf-8") as handle:
    for row in rows:
        handle.write(json.dumps(row) + "\n")
os.chmod(state / "ledger.jsonl", 0o600)

# Non-terminal tasks keep every dispatch pending, like a real backlog.
m._broker_task_on = lambda config, row, task, secret: {"status": "running"}

config = m._config(env)
assert config.collect_window == 32, config.collect_window
processed = m._process_verdicts(config, dry_run=False)
polled = [p["task_id"] for p in processed if p.get("outcome") == "verdict-pending"]
assert polled == [f"task-{i:02d}" for i in range(32)], polled[:4]
overflow = [p for p in processed if p.get("outcome") == "verdict-window-overflow"]
assert len(overflow) == 1, processed
assert overflow[0]["remaining"] == 8 and overflow[0]["oldest_ts"] == "2026-09-01T00:32:00Z", overflow[0]

# Env override is honored and bounded.
small = m._config({**env, "CCC_SKILL_PROMOTION_COLLECT_WINDOW": "5"})
assert small.collect_window == 5, small.collect_window
big = m._config({**env, "CCC_SKILL_PROMOTION_COLLECT_WINDOW": "999"})
assert big.collect_window == 32, big.collect_window

# Revise-result collection overflows symmetrically (a2a-revise-dispatch rows).
with open(state / "ledger.jsonl", "a", encoding="utf-8") as handle:
    for i in range(40):
        handle.write(json.dumps({
            "ts": f"2026-09-02T00:{i:02d}:00Z", "kind": "a2a-revise-dispatch",
            "task_id": f"rtask-{i:02d}", "pr": "9", "head_sha": "f" * 40,
            "node": "testnode", "name": f"win-skill-{i:012x}",
            "provider": "claude", "tree_sha256": "e" * 64,
        }) + "\n")
results = m._consume_revise_results(config, dry_run=True)
revise_overflow = [r for r in results if r.get("outcome") == "revise-window-overflow"]
assert len(revise_overflow) == 1 and revise_overflow[0]["remaining"] == 8, revise_overflow
would = [r for r in results if r.get("outcome") == "would-poll-revise"]
assert len(would) == 32 and would[0]["task_id"] == "rtask-00", would[:2]
print("WINDOW-FIFO-OK")
FIXTURE
env "${base_env[@]}" python3 "$WINDOW_FIXTURE" "$PROMOTER" > "$TMP/window-out" 2>&1; rc=$?
ok "collect window is FIFO, drains 32/run, and surfaces overflow (#1394)" \
  '[ "$rc" = 0 ] && grep -q "WINDOW-FIFO-OK" "$TMP/window-out"'

# --- 2026-09 collect-gap repair: T1/T2 broker routing gate -----------------
GATE_FIXTURE="$TMP/gate-fixture.py"
cat > "$GATE_FIXTURE" <<FIXTURE
import importlib.util, json, os, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("csp_gate", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["csp_gate"] = m
spec.loader.exec_module(m)

# --- _task_bor_matches semantics: absent bor keeps the gate open -----------
assert m._task_bor_matches({}, "seoseo") is True
assert m._task_bor_matches({"brokerOfRecord": "seoseo"}, "seoseo") is True
assert m._task_bor_matches({"brokerOfRecord": " gwakga "}, "gwakga") is True
assert m._task_bor_matches({"brokerOfRecord": "gwakga"}, "seoseo") is False

# --- _broker_task_gated resolutions over a stubbed broker pool -------------
class Cfg:
    remote_brokers = ({"name": "t2", "ssh_host": "h", "broker_url": "http://127.0.0.1:8787/",
                       "nexus_dir": "/n", "secret_cmd": "c"},)

row_t2 = {"broker": "t2", "broker_id": "gwakga"}
row_primary = {"broker": "primary", "broker_id": "seoseo"}

# claimed: the fetched task self-reports the row's claimed broker id.
m._broker_task_on = lambda config, row, task_id, secret: {
    "status": "succeeded", "brokerOfRecord": "gwakga"}
m._broker_id = lambda config, secret: "seoseo"
m._remote_broker_id = lambda config, rb: "gwakga"
task, resolution = m._broker_task_gated(Cfg(), row_t2, "t-1", "s")
assert resolution == "claimed" and task["status"] == "succeeded", (task, resolution)

# mismatch: task found on the claimed broker but it names another owner, and
# no other broker serves the same id -> visible mismatch, never a verdict.
m._broker_task_on = lambda config, row, task_id, secret: {
    "status": "succeeded", "brokerOfRecord": "seoseo"}
m._task_readback_on = lambda config, rb, task_id, secret: (_ for _ in ()).throw(
    m.PromotionError("dispatch_broker_unreachable"))
task, resolution = m._broker_task_gated(Cfg(), row_t2, "t-2", "s")
assert resolution == "mismatch" and task["brokerOfRecord"] == "seoseo", (task, resolution)

# corrected: the claimed broker 404s, the OTHER broker serves the task and
# self-reports its own id (the pr104 misrouted-row case).
m._broker_task_on = lambda config, row, task_id, secret: (_ for _ in ()).throw(
    m.PromotionError("dispatch_broker_unreachable"))
m._task_readback_on = lambda config, rb, task_id, secret: {
    "status": "succeeded", "brokerOfRecord": "gwakga"} if rb is not None else (_ for _ in ()).throw(
    m.PromotionError("dispatch_broker_unreachable"))
task, resolution = m._broker_task_gated(Cfg(), row_primary, "t-3", "s")
assert resolution == "corrected:t2" and task["brokerOfRecord"] == "gwakga", (task, resolution)

# missing: unreachable everywhere.
m._task_readback_on = lambda config, rb, task_id, secret: (_ for _ in ()).throw(
    m.PromotionError("dispatch_broker_unreachable"))
task, resolution = m._broker_task_gated(Cfg(), row_t2, "t-4", "s")
assert task is None and resolution == "missing", (task, resolution)
print("GATE-UNIT-OK")

# --- _process_verdicts records broker-mismatch instead of malformed -------
iso_state = Path(tempfile.mkdtemp()) / "skill-promotion"
iso_state.mkdir(parents=True)
env = dict(os.environ)
env["CCC_STATE_DIR"] = str(iso_state.parent)
config = m._config(env)
row = {
    "ts": "2026-09-04T00:00:00Z", "kind": "a2a-dispatch",
    "transport_id": "testnode-claude-gate-skill-0123456789ab",
    "head_sha": "a" * 40, "round_id": "r", "task_id": "gt-1",
    "dispatched_task": "gt-1", "reviewer_node": "reviewer1",
    "broker_id": "gwakga", "broker": "t2",
    "pr_url": "https://github.com/test/repo/pull/7",
    "node": "testnode", "provider": "claude", "name": "gate-skill",
    "tree_sha256": "b" * 64, "branch": "skill-intake/testnode/gate-skill",
}
with open(iso_state / "ledger.jsonl", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(row) + chr(10))
os.chmod(iso_state / "ledger.jsonl", 0o600)

# A contradicting brokerOfRecord must surface as broker-mismatch, not as a
# silent malformed verdict drop.
m._broker_task_on = lambda config, r, task_id, secret: {
    "status": "succeeded", "brokerOfRecord": "seoseo",
    "result": {"output": {"verdict": "approve", "findings": [], "head_sha": "a" * 40}},
}
m._task_readback_on = lambda config, rb, task_id, secret: (_ for _ in ()).throw(
    m.PromotionError("dispatch_broker_unreachable"))
m._broker_id = lambda config, secret: "seoseo"
m._remote_broker_id = lambda config, rb: "gwakga"
processed = m._process_verdicts(config, dry_run=False)
assert any(p.get("outcome") == "verdict-broker-mismatch" for p in processed), processed
verdict_rows = [json.loads(l) for l in open(iso_state / "ledger.jsonl")]
assert any(r.get("status") == "broker-mismatch" for r in verdict_rows), verdict_rows
assert not any(r.get("status") == "consumed" for r in verdict_rows), verdict_rows
print("GATE-CONSUME-OK")
FIXTURE
env "${base_env[@]}" python3 "$GATE_FIXTURE" "$PROMOTER" > "$TMP/gate-out" 2>&1; rc=$?
ok "broker routing gate: bor semantics, gated fetch resolutions, visible mismatch consumption" \
  '[ "$rc" = 0 ] && grep -q "GATE-UNIT-OK" "$TMP/gate-out" && grep -q "GATE-CONSUME-OK" "$TMP/gate-out"'

# --- 2026-09-04 #1470: signed receipt projection at consumption -----------
RECEIPT_FIXTURE="$TMP/receipt-fixture.py"
cat > "$RECEIPT_FIXTURE" <<'FIXTURE'
import importlib.util, json, os, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location("csp_rcpt", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["csp_rcpt"] = m
spec.loader.exec_module(m)

iso_state = Path(tempfile.mkdtemp()) / "skill-promotion"
iso_state.mkdir(parents=True)
env = dict(os.environ)
env["CCC_STATE_DIR"] = str(iso_state.parent)
config = m._config(env)

class Cfg:
    remote_brokers = ()
m._broker_task_gated = lambda config, row, task_id, secret: ({
    "status": "succeeded",
    "brokerOfRecord": "seoseo",
    "result": {
        "summary": "skills intake review: approve (1 finding(s))",
        "output": {
            "taskId": "gt-2", "verdict": "approve", "skillName": "gate-skill",
            "sourceTreeSha256": "c" * 64, "headPrefix": "aaaaaaa",
            "head_sha": "a" * 40, "head_sha2": "a" * 40,
            "rubric_version": "2026-08-28.2", "findings": [],
            "reviewer_node": "reviewer1",
        },
        "validations": [{"kind": "review", "nodeId": "reviewer1", "verdict": "pass"}],
        "provenance": {"schemaVersion": "a2a.result.provenance.v1",
                       "workerKeyId": "worker:reviewer1:g2:v1"},
    },
}, "claimed")
posted = []
m._pr_comment = lambda config, pr, body: posted.append((pr, body))

row = {
    "ts": "2026-09-04T00:00:00Z", "kind": "a2a-dispatch",
    "transport_id": "testnode-claude-gate-skill-0123456789ab",
    "head_sha": "a" * 40, "round_id": "r", "task_id": "gt-2",
    "dispatched_task": "gt-2", "reviewer_node": "reviewer1",
    "broker_id": "seoseo", "broker": "primary",
    "pr_url": "https://github.com/test/repo/pull/8",
    "node": "authornode", "provider": "claude", "name": "gate-skill",
    "tree_sha256": "c" * 64, "branch": "skill-intake/authornode/gate-skill",
}
with open(iso_state / "ledger.jsonl", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(row) + chr(10))
os.chmod(iso_state / "ledger.jsonl", 0o600)

processed = m._process_verdicts(config, dry_run=False)
assert any(p.get("outcome") == "verdict-consumed" for p in processed), processed
# Receipt fence posted BEFORE the verdict comment, with bindings.
assert len(posted) == 2, [b[:80] for _, b in posted]
receipt_body = posted[0][1]
assert receipt_body.index("Signed A2A intake review receipt") < receipt_body.index("```json"), receipt_body
assert '"task_id": "gt-2"' in receipt_body
assert '"lane": "skills_intake_review"' in receipt_body
assert '"head_sha": "' + "a" * 40 + '"' in receipt_body
assert '"author_node": "authornode"' in receipt_body
assert '"reviewer_node": "reviewer1"' in receipt_body
assert '"provenance"' in receipt_body
# Verdict comment still posts, and the approve note reflects the projected receipt.
verdict_body = posted[1][1]
assert "signed receipt projected on this PR" in verdict_body, verdict_body
# Ledger row consumed once with the receipt marker absent from verdict rows.
verdict_rows = [json.loads(l) for l in open(iso_state / "ledger.jsonl")]
assert sum(1 for r in verdict_rows if r.get("status") == "consumed") == 1, verdict_rows
print("RECEIPT-PROJECTION-OK")
FIXTURE
env "${base_env[@]}" python3 "$RECEIPT_FIXTURE" "$PROMOTER" > "$TMP/receipt-out" 2>&1; rc=$?; ok "signed receipt fence is projected at verdict consumption (#1470)" \
  '[ "$rc" = 0 ] && grep -q "RECEIPT-PROJECTION-OK" "$TMP/receipt-out"'

echo "PASS=$pass FAIL=$fail"
python3 "$HERE/ccc_skill_receipt_retry_test.py" || fail=$((fail+1))
[ "$fail" -eq 0 ]
