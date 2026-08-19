#!/usr/bin/env bash
# Hermetic tests for claude/hooks/nunchi/codex-feed.sh hardening: the bounded
# codex exec kill (TERM + SIGKILL escalation, detached stdin) and the
# stale-lane sweep that prevents orphaned codex processes from accumulating
# across cron ticks (fleet incident: 7 orphaned codex.bin processes aged
# 2026-08-04..08-16 found on daegyo 2026-08-19).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
FEED="$ROOT/claude/hooks/nunchi/codex-feed.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# fake codex: records argv + pid, then sleeps past the test timeout
mkdir -p "$TMP/bin"
cat > "$TMP/bin/codex" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${CODEX_ARGV_FILE:?}"
echo "$$" >> "${CODEX_PID_FILE:?}"
sleep 600
SH
chmod 0700 "$TMP/bin/codex"

STATE="$TMP/state"; NUNCHI_HOME="$TMP/nunchi"; SESSIONS="$TMP/sessions"
mkdir -p "$STATE" "$NUNCHI_HOME" "$SESSIONS"
echo on > "$STATE/nunchi.mode"

# one rollout file with a >200-char conversation
python3 - "$SESSIONS" <<'PY'
import json, os, sys
d = sys.argv[1]
lines = []
for i in range(30):
    lines.append({"type": "event_msg", "payload": {"type": "user_message", "message": f"user says something meaningful number {i} with enough words to cross the threshold"}})
    lines.append({"type": "event_msg", "payload": {"type": "agent_message", "message": f"agent replies with substance number {i} and more words to be sure"}})
with open(os.path.join(d, "rollout-2026-08-19-aa.jsonl"), "w") as fh:
    for line in lines:
        fh.write(json.dumps(line) + "\n")
PY
rollout="$SESSIONS/rollout-2026-08-19-aa.jsonl"

# stale lane process from an "earlier tick": a fake codex carrying the lane tag
PATH="$TMP/bin:$PATH" CODEX_ARGV_FILE="$TMP/stale-argv" CODEX_PID_FILE="$TMP/stale-pid" \
  setsid "$TMP/bin/codex" exec dummy-prompt "[nunchi-codex-feed-816]" >/dev/null 2>&1 &
spawner=$!
sleep 1
stale_pid="$(tail -n 1 "$TMP/stale-pid" 2>/dev/null || true)"
ok "stale lane fixture is alive before the feed runs" '[ -n "$stale_pid" ] && kill -0 "$stale_pid" 2>/dev/null'

argv_file="$TMP/codex-argv"; pid_file="$TMP/codex-pid"
PATH="$TMP/bin:$PATH" \
CCC_STATE_DIR="$STATE" NUNCHI_HOME="$NUNCHI_HOME" CODEX_SESSIONS_DIR="$SESSIONS" \
NUNCHI_FEED_CODEX_TIMEOUT_SEC=2 NUNCHI_FEED_CODEX_KILL_GRACE_SEC=3 \
CODEX_ARGV_FILE="$argv_file" CODEX_PID_FILE="$pid_file" \
  bash "$FEED" >/dev/null 2>&1
feed_rc=$?

ok "feed run completes despite a hanging codex" '[ "$feed_rc" = 0 ]'
ok "stale lane process is swept at feed start" '! kill -0 "$stale_pid" 2>/dev/null'
lane_pid="$(tail -n 1 "$pid_file" 2>/dev/null || true)"
ok "this run's codex exec is killed by the bounded timeout" '[ -n "$lane_pid" ] && ! kill -0 "$lane_pid" 2>/dev/null'
ok "codex exec argv carries the lane tag" 'grep -q "nunchi-codex-feed-816" "$argv_file"'
ok "processed rollout is marked seen despite the non-JSON response" 'grep -qxF "$rollout" "$NUNCHI_HOME/codex-seen"'
ok "stdin was detached (fake codex did not inherit the test stdin)" '[ "$(wc -l < "$pid_file")" -ge 1 ]'

kill "$spawner" 2>/dev/null || true
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
