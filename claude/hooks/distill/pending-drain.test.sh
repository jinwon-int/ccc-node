#!/usr/bin/env bash
# Durable distill enqueue/recovery tests. Provider and network are stubbed.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
DISTILL="$HERE/../distill.sh"
DRAIN="$HERE/pending-drain.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"

pass=0; fail=0
TMP="$(mktemp -d)"
trap 'pgid="$(cat "$TMP/claude.pgid" 2>/dev/null || true)"; [ -z "$pgid" ] || kill -KILL -- "-$pgid" 2>/dev/null || true; rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
wait_for() {
  local test_cmd="$1"
  for _ in $(seq 1 100); do
    eval "$test_cmd" && return 0
    sleep 0.05
  done
  return 1
}

STATE="$TMP/state"
PROJECT="$TMP/projects/-root-workspace"
mkdir -p "$STATE" "$PROJECT" "$TMP/bin"
touch "$STATE/distill.dryrun"

make_transcript() {
  local path="$1" marker="$2"
  : > "$path"
  for i in 1 2 3; do
    printf '{"type":"user","message":{"content":"%s turn %s"}}\n' "$marker" "$i" >> "$path"
  done
}

payload() {
  jq -nc --arg sid "$1" --arg tp "$2" \
    '{session_id:$sid, transcript_path:$tp, cwd:"/root/workspace"}'
}

write_exec_stub "$TMP/bin/claude" <<'SH'
mode="$(cat "${CLAUDE_STUB_MODE_FILE:?}" 2>/dev/null || printf success)"
cat >/dev/null
case "$mode" in
  sleep)
    ps -o pgid= -p "$$" | tr -d '[:space:]' > "${CLAUDE_STUB_PGID_FILE:?}"
    sleep 30
    ;;
  fail) exit 9 ;;
  *)
    printf '%s' '{"honcho":[],"wiki_candidates":[],"resume":{"last_activity":"ok","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}'
    ;;
esac
SH
chmod +x "$TMP/bin/claude"
export PATH="$TMP/bin:$PATH"
export CLAUDE_STUB_MODE_FILE="$TMP/claude.mode"
export CLAUDE_STUB_PGID_FILE="$TMP/claude.pgid"

TRANSCRIPT="$PROJECT/sess-durable.jsonl"
make_transcript "$TRANSCRIPT" durable
printf '%s\n' sleep > "$CLAUDE_STUB_MODE_FILE"

payload sess-durable "$TRANSCRIPT" | \
  HOME="$TMP/home" CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root/workspace" \
  CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 \
  bash "$DISTILL" sessionend >/dev/null 2>&1

wait_for '[ "$(find "$STATE/distill-pending" -maxdepth 1 -type f -name "*.json" | wc -l)" = 1 ]'
job="$(find "$STATE/distill-pending" -maxdepth 1 -type f -name '*.json' | head -1)"
wait_for '[ -s "$CLAUDE_STUB_PGID_FILE" ]'
ok "SessionEnd writes one durable job before provider completion" '[ -f "$job" ] && grep -q "enqueued job=" "$STATE/distill.log"'
ok "pending directory and job are owner-only" '[ "$(stat -c %a "$STATE/distill-pending")" = 700 ] && [ "$(stat -c %a "$job")" = 600 ]'
ok "job captures external isolation for recovery" 'jq -e '\''.schema == "ccc.distill.pending.v1" and .isolation_profile == "external" and .wiki_memory_enabled == "1"'\'' "$job" >/dev/null'

# A repeated trigger for the same transcript hash must not create another job.
payload sess-durable "$TRANSCRIPT" | \
  HOME="$TMP/home" CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root/workspace" \
  CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 \
  bash "$DISTILL" sessionend >/dev/null 2>&1
sleep 0.1
ok "same transcript snapshot deduplicates to one job" \
  '[ "$(find "$STATE/distill-pending" -maxdepth 1 -type f -name "*.json" | wc -l)" = 1 ] && grep -q "enqueue dedup job=" "$STATE/distill.log"'

# Simulate teardown during extraction. The job must survive after the killed
# provider subprocess causes its worker to fail.
kill -KILL -- "-$(cat "$CLAUDE_STUB_PGID_FILE")" 2>/dev/null || true
wait_for 'flock -n "$job.lock" true 2>/dev/null'
ok "killed extraction retains the durable job" '[ -f "$job" ]'

# The next SessionStart recovery pass launches the retained job and removes it
# only after a successful extraction.
recovered_job_complete() {
  [ ! -f "$job" ] &&
    [ ! -e "$job.lock" ] &&
    grep -q "\[pending-drain\] spawned job=" "$STATE/distill.log" &&
    grep -q "pending completed job=" "$STATE/distill.log"
}

printf '%s\n' success > "$CLAUDE_STUB_MODE_FILE"
HOME="$TMP/home" CCC_STATE_DIR="$STATE" bash "$DRAIN" >/dev/null 2>&1
wait_for 'recovered_job_complete'
ok "SessionStart drain completes and removes a recovered job" \
  'recovered_job_complete'

# A genuine extraction failure remains retryable rather than being consumed.
TRANSCRIPT_FAIL="$PROJECT/sess-fail.jsonl"
make_transcript "$TRANSCRIPT_FAIL" failure
printf '%s\n' fail > "$CLAUDE_STUB_MODE_FILE"
payload sess-fail "$TRANSCRIPT_FAIL" | \
  HOME="$TMP/home" CCC_STATE_DIR="$STATE" CCC_DISTILL_SCOPE_CWDS="/root/workspace" \
  bash "$DISTILL" sessionend >/dev/null 2>&1
wait_for 'grep -q "pending retained reason=pipeline-failed" "$STATE/distill.log" && [ "$(find "$STATE/distill-pending" -maxdepth 1 -type f -name "*.json" | wc -l)" = 1 ]'
ok "failed extraction keeps one retryable pending job" \
  '[ "$(find "$STATE/distill-pending" -maxdepth 1 -type f -name "*.json" | wc -l)" = 1 ]'

# setup.sh deploys (and chmods) the whole claude/hooks tree via the shared
# hook-tree walk (#569) — assert the launcher is in the walk's deployable set.
ok "setup installs and chmods the recovery launcher" \
  '. "$ROOT/scripts/lib/harness-paths.sh" && ccc_hook_tree_files "$ROOT" | grep -Fxq "distill/pending-drain.sh" && grep -Fq '\''ccc_hook_tree_files "$SRC"'\'' "$ROOT/setup.sh"'
ok "SessionStart schedules bounded pending recovery" \
  'jq -e '\''[.hooks.SessionStart[].hooks[].command] | any(contains("distill/pending-drain.sh"))'\'' "$ROOT/claude/settings.base.json" >/dev/null'

# --- fleet autonomy guard (#386): kill drains nothing, dry-run proceeds -------
# One retryable pending job is on disk from the failure case above. Under kill
# the launcher must spawn nothing and retain the job; under dry-run it proceeds.
: > "$STATE/distill.log"
before_jobs="$(find "$STATE/distill-pending" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d '[:space:]')"
ok "precondition: a pending job exists to drain" '[ "$before_jobs" -ge 1 ]'
HOME="$TMP/home" CCC_STATE_DIR="$STATE" CCC_AUTONOMY=kill bash "$DRAIN" >/dev/null 2>&1
ok "autonomy=kill drain spawns nothing" '! grep -q "\[pending-drain\] spawned job=" "$STATE/distill.log"'
ok "autonomy=kill drain logs skip reason" 'grep -q "\[pending-drain\] skip reason=autonomy-kill" "$STATE/distill.log"'
ok "autonomy=kill drain retains pending jobs" \
  '[ "$(find "$STATE/distill-pending" -maxdepth 1 -type f -name "*.json" | wc -l | tr -d " ")" = "$before_jobs" ]'

: > "$STATE/distill.log"
printf '%s\n' success > "$CLAUDE_STUB_MODE_FILE"
HOME="$TMP/home" CCC_STATE_DIR="$STATE" CCC_AUTONOMY=dry-run bash "$DRAIN" >/dev/null 2>&1
ok "autonomy=dry-run drain is not halted (reaches spawn)" \
  'wait_for '\''grep -q "\[pending-drain\] spawned job=" "$STATE/distill.log"'\'' && ! grep -q "skip reason=autonomy-kill" "$STATE/distill.log"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
