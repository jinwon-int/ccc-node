#!/usr/bin/env bash
# harness: umask-rerun
# shellcheck disable=SC2034  # rc/crc/jrc*/out are read via eval inside ok()
# Tests for distill.sh resume ownership (#2319) — no provider/network calls.
#
# resume.md is the single "직전 세션에서 이어서" source. The bridge sink owns it
# for the owner's main thread; this hook used to let any other session that
# ended on the node (agent-cron headless runs, A2A review lanes, a non-bridge
# process re-opening a bridge transcript under a new session id) overwrite it.
# The fix resolves a per-session policy — write (default) | skip:<reason> —
# and in skip mode commits facts only.
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DISTILL="$HERE/distill.sh"
COMMIT="$HERE/distill/local-memory-commit.py"
JOURNAL="$HERE/distill/pending_journal.py"
HEADLESS="$ROOT/claude/headless.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/lib/test-stub.sh"
ccc_test_reset_hook_env
# Hermetic against operator shells: audience-scoped nodes export these globally
# and local-memory-commit.py rejects facts/resume paths outside CCC_STATE_DIR.
unset CCC_MEMORY_FACTS_FILE CCC_RESUME_FILE CCC_MEMORY_INDEX_DB CCC_MEMORY_CACHE_DIR \
      CCC_MEMORY_AUDIENCE CCC_MEMORY_AUDIENCE_SCOPED CCC_MEMORY_AUDIENCE_ROOT \
      CCC_MEMORY_SCOPE CCC_DISTILL_RESUME_POLICY CLAUDE_DISTILL_RESUME_POLICY \
      CCC_BRIDGE_SESSION_STORES
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_transcript() {
  local path="$1" lines="${2:-6}"
  mkdir -p "$(dirname "$path")"
  : > "$path"
  for i in $(seq 1 "$lines"); do
    printf '{"type":"user","message":{"content":"turn %s"}}\n' "$i" >> "$path"
  done
}

payload() {
  jq -nc --arg sid "$1" --arg tp "$2" --arg cwd "$3" \
    '{session_id:$sid, transcript_path:$tp, cwd:$cwd}'
}

# Stub provider: one fact + one resume per extraction, no network.
mkdir -p "$TMP/bin"
write_exec_stub "$TMP/bin/claude" <<'SH'
cat >/dev/null
printf '{"honcho":[{"kind":"fact","text":"stub fact %s","subject":"node"}],"wiki_candidates":[],"resume":{"last_activity":"hook wrote this","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}' "$$"
SH
write_exec_stub "$TMP/bin/setsid" <<'SH'
exec "$@"
SH
PATH="$TMP/bin:$PATH"

HOME_T="$TMP/home"; mkdir -p "$HOME_T"
export HOME="$HOME_T"
export CCC_WIKI_MEMORY_ENABLED=0
CWD="/root/.openclaw/workspace"
PROJ="$TMP/projects/-root--openclaw-workspace"

BRIDGE_RESUME='<!-- ccc-node:distill captured_at=2026-10-15T01:16:45Z schema=1 provider=claude thread_hash=abc trigger=auto_new -->
- 마지막 작업: bridge thread handoff
'

# run_case <state-dir> <session-id> <transcript-path> [env assignments...]
run_case() {
  local state="$1" sid="$2" tp="$3"; shift 3
  mkdir -p "$state"; : > "$state/distill.log"
  payload "$sid" "$tp" "$CWD" \
    | env CCC_STATE_DIR="$state" CCC_DISTILL_SCOPE_CWDS="$CWD" "$@" bash "$DISTILL" sessionend >/dev/null 2>&1
  rc=$?
  for _ in $(seq 1 50); do
    grep -q "done trigger=sessionend" "$state/distill.log" && break
    sleep 0.1
  done
}

# ---- 1. default: an ordinary CLI session still writes resume + facts --------
S1="$TMP/s1"; T1="$PROJ/sess-plain.jsonl"; make_transcript "$T1" 4
run_case "$S1" sess-plain "$T1"
ok "default: hook exits 0" '[ "$rc" = 0 ]'
ok "default: pipeline completed" 'grep -q "done trigger=sessionend" "$S1/distill.log"'
ok "default: resume written by hook" 'grep -q "hook wrote this" "$S1/resume.md"'
ok "default: facts appended" '[ -s "$S1/memory-facts.jsonl" ]'
ok "default: no skip verdict logged" '! grep -q "resume policy=skip" "$S1/distill.log" && grep -q "mode-both\|committed" "$S1/distill.log"'

# ---- 2. bridge-owned transcript: transcript basename is a bridge session ----
S2="$TMP/s2"; mkdir -p "$S2" "$HOME_T/.ccc-matrix"
T2="$PROJ/sess-bridge.jsonl"; make_transcript "$T2" 4
jq -nc '{"telegram_session:1": {provider:"claude", session_id:"sess-bridge"}}' > "$HOME_T/.ccc-matrix/sessions.json"
printf '%s' "$BRIDGE_RESUME" > "$S2/resume.md"
# The c33b9d8f shape: a new session id re-opened the bridge thread's transcript.
run_case "$S2" sess-reopened "$T2"
ok "bridge-owned: hook exits 0" '[ "$rc" = 0 ]'
ok "bridge-owned: verdict logged with reason" 'grep -q "resume policy=skip reason=bridge-owned-transcript session=sess-reopened" "$S2/distill.log"'
ok "bridge-owned: commit ran facts-only" 'grep -q "local-memory mode=facts reason=resume-policy policy=skip:bridge-owned-transcript" "$S2/distill.log"'
ok "bridge-owned: resume.md untouched" '[ "$(cat "$S2/resume.md")" = "$(printf '%s' "$BRIDGE_RESUME")" ]'
ok "bridge-owned: facts still appended" 'grep -q "stub fact" "$S2/memory-facts.jsonl"'

# ---- 3. bridge-owned via hook session id (store lists the hook's own id) ----
S3="$TMP/s3"; mkdir -p "$S3" "$HOME_T/.telegram_bot"
T3="$PROJ/sess-other-file.jsonl"; make_transcript "$T3" 4
jq -nc '{"telegram_session:7": {session_id:"sess-tg-main", dead_session_wakeup:{session_id:"sess-dead"}}}' > "$HOME_T/.telegram_bot/sessions.json"
printf '%s' "$BRIDGE_RESUME" > "$S3/resume.md"
run_case "$S3" sess-tg-main "$T3"
ok "session-id match: verdict logged" 'grep -q "resume policy=skip reason=bridge-owned-transcript" "$S3/distill.log"'
ok "session-id match: resume.md untouched" '[ "$(cat "$S3/resume.md")" = "$(printf '%s' "$BRIDGE_RESUME")" ]'

# ---- 4. explicit opt-out (what headless.sh exports) -------------------------
S4="$TMP/s4"; mkdir -p "$S4"
T4="$PROJ/sess-cron.jsonl"; make_transcript "$T4" 4
printf '%s' "$BRIDGE_RESUME" > "$S4/resume.md"
run_case "$S4" sess-cron "$T4" CCC_DISTILL_RESUME_POLICY=skip
ok "env skip: verdict logged" 'grep -q "resume policy=skip reason=env" "$S4/distill.log"'
ok "env skip: resume.md untouched" '[ "$(cat "$S4/resume.md")" = "$(printf '%s' "$BRIDGE_RESUME")" ]'
ok "env skip: facts still appended" 'grep -q "stub fact" "$S4/memory-facts.jsonl"'

# ---- 5. unrelated store entries do not trip the guard -----------------------
S5="$TMP/s5"; mkdir -p "$S5"
T5="$PROJ/sess-free.jsonl"; make_transcript "$T5" 4
run_case "$S5" sess-free "$T5"
ok "unrelated store: resume written" 'grep -q "hook wrote this" "$S5/resume.md"'
ok "unrelated store: no skip verdict" '! grep -q "resume policy=skip" "$S5/distill.log"'

# ---- 6. custom store list + unreadable store are tolerated ------------------
S6="$TMP/s6"; mkdir -p "$S6"
T6="$PROJ/sess-custom.jsonl"; make_transcript "$T6" 4
jq -nc '{a:{session_id:"sess-custom"}}' > "$TMP/custom-sessions.json"
run_case "$S6" sess-x "$T6" CCC_BRIDGE_SESSION_STORES="$TMP/missing.json:$TMP/custom-sessions.json"
ok "custom store: verdict from CCC_BRIDGE_SESSION_STORES" 'grep -q "resume policy=skip reason=bridge-owned-transcript" "$S6/distill.log"'
ok "custom store: missing path tolerated" '[ ! -e "$S6/resume.md" ] && grep -q "stub fact" "$S6/memory-facts.jsonl"'

# ---- 7. pending journal carries the verdict into replays --------------------
PD="$TMP/pending"; mkdir -p "$PD"
TJ="$PROJ/sess-journal.jsonl"; make_transcript "$TJ" 4
rec="$(CLAUDE_DISTILL_TRANSCRIPT="$TJ" CLAUDE_DISTILL_SESSION=sess-journal \
  CLAUDE_DISTILL_RESUME_POLICY=skip:bridge-owned-transcript CLAUDE_DISTILL_TRIGGER=sessionend \
  python3 "$JOURNAL" enqueue "$PD" 2>/dev/null)"
job="$(printf '%s' "$rec" | jq -r '.job_id // empty')"
ok "journal: enqueue returns a job" '[ -n "$job" ] && [ -f "$PD/$job.json" ]'
ok "journal: record stores resume_policy" '[ "$(jq -r .resume_policy "$PD/$job.json")" = "skip:bridge-owned-transcript" ]'
ENVDUMP="$TMP/child.env"
write_exec_stub "$TMP/fake-distill.sh" <<SH
env > "$ENVDUMP"
exit 76
SH
python3 "$JOURNAL" run "$PD" "$PD/$job.json" "$TMP/fake-distill.sh" >/dev/null 2>&1; jrc=$?
ok "journal: replay completes" '[ "$jrc" = 0 ]'
ok "journal: replay child inherits the verdict" 'grep -q "^CLAUDE_DISTILL_RESUME_POLICY=skip:bridge-owned-transcript$" "$ENVDUMP"'
# A record written before #2319 (no resume_policy) must still validate and
# default to write.
PD2="$TMP/pending2"; mkdir -p "$PD2"
rec2="$(CLAUDE_DISTILL_TRANSCRIPT="$TJ" CLAUDE_DISTILL_SESSION=sess-legacy CLAUDE_DISTILL_TRIGGER=sessionend \
  python3 "$JOURNAL" enqueue "$PD2" 2>/dev/null)"
job2="$(printf '%s' "$rec2" | jq -r '.job_id // empty')"
jq 'del(.resume_policy)' "$PD2/$job2.json" > "$PD2/tmp.json" && mv "$PD2/tmp.json" "$PD2/$job2.json"
python3 "$JOURNAL" run "$PD2" "$PD2/$job2.json" "$TMP/fake-distill.sh" >/dev/null 2>&1; jrc2=$?
ok "journal: legacy record (no resume_policy) still replays" '[ "$jrc2" = 0 ]'
ok "journal: legacy record defaults to write" 'grep -q "^CLAUDE_DISTILL_RESUME_POLICY=write$" "$ENVDUMP"'
# Garbage values are rejected rather than silently trusted.
PD3="$TMP/pending3"; mkdir -p "$PD3"
rec3="$(CLAUDE_DISTILL_TRANSCRIPT="$TJ" CLAUDE_DISTILL_SESSION=sess-bad CLAUDE_DISTILL_TRIGGER=sessionend \
  python3 "$JOURNAL" enqueue "$PD3" 2>/dev/null)"
job3="$(printf '%s' "$rec3" | jq -r '.job_id // empty')"
jq '.resume_policy = "skip:$(rm -rf /)"' "$PD3/$job3.json" > "$PD3/tmp.json" && mv "$PD3/tmp.json" "$PD3/$job3.json"
python3 "$JOURNAL" run "$PD3" "$PD3/$job3.json" "$TMP/fake-distill.sh" >/dev/null 2>&1; jrc3=$?
ok "journal: malformed resume_policy is an invalid job (74)" '[ "$jrc3" = 74 ]'

# ---- 8. local-memory-commit honors the env directly -------------------------
SC="$TMP/sc"; mkdir -p "$SC"
printf '%s' "$BRIDGE_RESUME" > "$SC/resume.md"
DOC='{"session_id":"sess-commit","trigger":"sessionend","honcho":[{"kind":"fact","text":"commit fact","subject":"x"}],"wiki_candidates":[],"resume":{"last_activity":"commit wrote this","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}'
out="$(printf '%s' "$DOC" | CCC_STATE_DIR="$SC" CLAUDE_DISTILL_RESUME_POLICY=skip:env python3 "$COMMIT" --mode both 2>&1)"; crc=$?
ok "commit: skip policy keeps exit 0" '[ "$crc" = 0 ]'
ok "commit: skip policy writes facts only" 'grep -q "commit fact" "$SC/memory-facts.jsonl" && [ "$(cat "$SC/resume.md")" = "$(printf '%s' "$BRIDGE_RESUME")" ]'
ok "commit: skip policy reports a facts-only transaction" 'grep -q "targets=memory-facts.jsonl" <<<"$out" && ! grep -q "resume.md" <<<"$out"'
out="$(printf '%s' "$DOC" | CCC_STATE_DIR="$SC" CCC_DISTILL_RESUME_POLICY=skip python3 "$COMMIT" --mode resume 2>&1)"; crc=$?
ok "commit: --mode resume under skip is a logged no-op" '[ "$crc" = 0 ] && grep -q "resume skipped reason=resume-policy" <<<"$out" && [ "$(cat "$SC/resume.md")" = "$(printf '%s' "$BRIDGE_RESUME")" ]'
out="$(printf '%s' "$DOC" | CCC_STATE_DIR="$SC" CLAUDE_DISTILL_RESUME_POLICY=write python3 "$COMMIT" --mode both 2>&1)"; crc=$?
ok "commit: write policy replaces resume" '[ "$crc" = 0 ] && grep -q "commit wrote this" "$SC/resume.md"'

# ---- 9. headless.sh marks its session as automated --------------------------
write_exec_stub "$TMP/bin/claude-headless-stub" <<'SH'
cat >/dev/null 2>&1 || true
printf '{"result":"policy=%s","session_id":"s","total_cost_usd":0}' "${CCC_DISTILL_RESUME_POLICY:-unset}"
SH
out="$(CCC_CLAUDE_BIN="$TMP/bin/claude-headless-stub" CCC_HEADLESS_TIMEOUT=0 bash "$HEADLESS" "ping" </dev/null 2>/dev/null)"
ok "headless: exports skip by default" 'grep -q "policy=skip" <<<"$out"'
out="$(CCC_CLAUDE_BIN="$TMP/bin/claude-headless-stub" CCC_HEADLESS_TIMEOUT=0 CCC_DISTILL_RESUME_POLICY=write bash "$HEADLESS" "ping" </dev/null 2>/dev/null)"
ok "headless: explicit caller value wins" 'grep -q "policy=write" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
