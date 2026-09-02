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
# Liveness tick (2026-09-02): the codex/piri feeds must leave the same
# ingest.status.json ingest-cron.sh does, or ccc-doctor's ingest-tick age
# check reads a provider-switched node's stale claude-era file as STALE.
ok "feed writes the ingest liveness tick" '[ -f "$NUNCHI_HOME/ingest.status.json" ]'
ok "tick carries the shared schema and the codex feed tag" 'jq -e ".schema == \"ccc.nunchi.ingest.v1\" and .feed == \"codex\" and (.sources|type) == \"number\"" "$NUNCHI_HOME/ingest.status.json" >/dev/null'
ok "tick finished_at is now-ish" '[ $(( $(date -u +%s) - $(jq -r .finished_at "$NUNCHI_HOME/ingest.status.json") )) -lt 600 ]'
PIRI_FEED="$ROOT/claude/hooks/nunchi/piri-feed.sh"
# --- piri-feed liveness + launcher resolution (2026-09-02, gongyung) --------
# (1) No runnable CLI anywhere: the lane must still leave a tick, tagged as
#     skipped, instead of exiting before the status write (doctor then aged the
#     previous tick into ingest-tick-stale and hid the real cause).
PN1="$TMP/pn1"; PS1="$TMP/psess1"; CL1="$TMP/claude-nolauncher"; mkdir -p "$PN1" "$PS1" "$CL1/hooks" "$TMP/bin-nopiri"
CCC_STATE_DIR="$STATE" NUNCHI_HOME="$PN1" PIR_SESSIONS_DIR="$PS1" CCC_CLAUDE_DIR="$CL1" \
  PATH="$TMP/bin-nopiri:/usr/bin:/bin" bash "$PIRI_FEED" >"$TMP/pf1.out" 2>"$TMP/pf1.err"; pf_rc=$?
ok "piri feed without any CLI exits 0" '[ "$pf_rc" = 0 ]'
ok "piri feed without any CLI names all three lookups in its notice" 'grep -q "no hooks/ccc-piri" "$TMP/pf1.err"'
ok "piri feed without any CLI still writes a skipped liveness tick" 'jq -e ".schema == \"ccc.nunchi.ingest.v1\" and .feed == \"piri\" and .skipped == \"cli-not-runnable\"" "$PN1/ingest.status.json" >/dev/null'
# (2) Launcher fallback: CCC_PIRI_CLI_PATH unset, piri not on PATH, but the
#     harness launcher hooks/ccc-piri exists → resolved, no skip notice.
PN2="$TMP/pn2"; PS2="$TMP/psess2"; CL2="$TMP/claude-launcher"; mkdir -p "$PN2" "$PS2" "$CL2/hooks"
write_exec_stub "$CL2/hooks/ccc-piri" <<'SH'
printf '{"honcho":[]}\n'
SH
CCC_STATE_DIR="$STATE" NUNCHI_HOME="$PN2" PIR_SESSIONS_DIR="$PS2" CCC_CLAUDE_DIR="$CL2" \
  PATH="$TMP/bin-nopiri:/usr/bin:/bin" bash "$PIRI_FEED" >"$TMP/pf2.out" 2>"$TMP/pf2.err"; pf_rc=$?
ok "piri feed resolves the harness launcher when env and PATH are empty" '[ "$pf_rc" = 0 ] && ! grep -q "not runnable" "$TMP/pf2.err"'
ok "resolved-launcher run writes a normal (unskipped) tick" 'jq -e ".schema == \"ccc.nunchi.ingest.v1\" and .feed == \"piri\" and (has(\"skipped\") | not)" "$PN2/ingest.status.json" >/dev/null'
ok "piri feed carries the same tick writer" 'grep -q "ccc.nunchi.ingest.v1" "$ROOT/claude/hooks/nunchi/piri-feed.sh" && grep -q "\"feed\":\"%s\"" "$ROOT/claude/hooks/nunchi/piri-feed.sh"'
ok "stale lane process is swept at feed start" '! kill -0 "$stale_pid" 2>/dev/null'
lane_pid="$(tail -n 1 "$pid_file" 2>/dev/null || true)"
ok "this run's codex exec is killed by the bounded timeout" '[ -n "$lane_pid" ] && ! kill -0 "$lane_pid" 2>/dev/null'
ok "codex exec argv carries the lane tag" 'grep -q "nunchi-codex-feed-816" "$argv_file"'
ok "processed rollout is marked seen despite the non-JSON response" 'grep -qxF "$rollout" "$NUNCHI_HOME/codex-seen"'
ok "stdin was detached (fake codex did not inherit the test stdin)" '[ "$(wc -l < "$pid_file")" -ge 1 ]'

kill "$spawner" 2>/dev/null || true

# #1264: both feed lanes must prompt for decision facts with a because reason,
# and the two prompt blocks must not drift apart (the legacy 4-kind prompt is
# how decision rationale never reached piri/codex nodes — bench q7).
ok "codex lane prompt requires decision+because" 'grep -q "decision" "$FEED" && grep -q "because" "$FEED"'
ok "piri lane prompt requires decision+because" 'grep -q "decision" "$PIRI_FEED" && grep -q "because" "$PIRI_FEED"'
ok "both feed lanes forbid inventing a missing decision reason" \
  'grep -q "추측하거나 지어내지 마라" "$FEED" && grep -q "추측하거나 지어내지 마라" "$PIRI_FEED"'
ok "both feed payloads declare the required-v1 reason contract" \
  'grep -q '\''"decision_reason_contract": "required-v1"'\'' "$FEED" && grep -q '\''"decision_reason_contract": "required-v1"'\'' "$PIRI_FEED"'
prompt_of() { awk "/^PROMPT_PREFIX='/{f=1;next} f && /^'/{exit} f{print}" "$1"; }
ok "feed prompt blocks stay byte-identical across lanes" \
  '[ "$(prompt_of "$FEED")" = "$(prompt_of "$PIRI_FEED")" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
