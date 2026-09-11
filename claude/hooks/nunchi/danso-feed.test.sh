#!/usr/bin/env bash
# Hermetic tests for claude/hooks/nunchi/danso-feed.sh (#1698) and the shared
# feed-common.sh drift reporting. Danso-provider nodes had no feed lane at all,
# so whichever feed was installed before the provider switch kept ticking
# happily against a dried-up source: gongmyoung 6 days and soonwook 43 days with
# `ingested: 0` and no finding anywhere. These tests pin the two behaviours that
# make that failure visible — a real mirror lane, and a tick that says when the
# lane and the runtime provider disagree.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
FEED="$ROOT/claude/hooks/nunchi/danso-feed.sh"
LIB="$ROOT/claude/hooks/nunchi/feed-common.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

STATE="$TMP/state"; NUNCHI_HOME="$TMP/nunchi"; JOURNAL="$TMP/danso-distill-journal"
mkdir -p "$STATE" "$NUNCHI_HOME" "$JOURNAL"
echo on > "$STATE/nunchi.mode"
DB="$TMP/facts.db"; SNAP="$TMP/snapshot.md"
STATUS="$NUNCHI_HOME/ingest.status.json"

# One finished bridge distill job, in the DistillJournal shape bridge-journal.py
# adapts. The danso lane writes the same job files as every other provider
# (bridge/__main__.py only swaps the directory name), which is exactly why this
# lane can mirror instead of paying for a second extraction.
python3 - "$JOURNAL" <<'PY'
import json, os, sys
root = sys.argv[1]
job = {
    "job_id": "job-danso-1",
    "thread_id": "11111111-2222-3333-4444-555555555555",
    "status": "extraction_done",
    "updated_at": "2026-09-12T00:00:00+00:00",
    "trigger": "checkpoint",
    "extraction_output": json.dumps({
        "honcho": [
            {"kind": "fact", "subject": "node",
             "text": "곽가 노드의 nunchi 피드는 danso 저널을 미러링한다."},
        ],
        "provenance": {"distilled_at": "2026-09-12T00:00:00+00:00",
                       "trigger": "checkpoint"},
    }),
}
with open(os.path.join(root, "job-danso-1.json"), "w", encoding="utf-8") as handle:
    json.dump(job, handle, ensure_ascii=False)
# An unfinished job must be deferred, not marked seen.
pending = {"job_id": "job-danso-2", "thread_id": "", "status": "running"}
with open(os.path.join(root, "job-danso-2.json"), "w", encoding="utf-8") as handle:
    json.dump(pending, handle)
PY

run_feed() {  # <extra env assignments...> — journal dir fixed, provider varies
  env CCC_STATE_DIR="$STATE" NUNCHI_HOME="$NUNCHI_HOME" NUNCHI_DB="$DB" \
    NUNCHI_SNAPSHOT="$SNAP" CCC_BRIDGE_DISTILL_JOURNAL="$JOURNAL" \
    "$@" bash "$FEED"
}

# --- 1. the mirror actually ingests, and reports the lane -------------------
run_feed CCC_AGENT_PROVIDER=danso >"$TMP/run1.out" 2>"$TMP/run1.err"
rc1=$?
ok "feed exits 0" "[ $rc1 = 0 ]"
ok "status file written" "[ -f '$STATUS' ]"
ok "tick tagged as the danso lane" \
  "jq -e '.schema == \"ccc.nunchi.ingest.v1\" and .feed == \"danso\" and (.sources|type) == \"number\"' '$STATUS' >/dev/null"
ok "tick is fresh" \
  "[ \$(( \$(date -u +%s) - \$(jq -r .finished_at '$STATUS') )) -lt 600 ]"
ok "the finished job was ingested" "jq -e '.ingested == 1' '$STATUS' >/dev/null"
ok "the in-flight job was deferred, not consumed" \
  "jq -e '.deferred == 1' '$STATUS' >/dev/null"
ok "finished job marked seen" \
  "grep -qxF '$JOURNAL/job-danso-1.json' '$NUNCHI_HOME/danso-seen'"
ok "in-flight job left unseen for a later tick" \
  "! grep -qxF '$JOURNAL/job-danso-2.json' '$NUNCHI_HOME/danso-seen'"
ok "no provider drift reported when lane and provider agree" \
  "! jq -e 'has(\"feed_provider_mismatch\")' '$STATUS' >/dev/null"
ok "matching provider still recorded" \
  "jq -e '.feed_provider == \"danso\"' '$STATUS' >/dev/null"

# --- 2. rerunning is idempotent -------------------------------------------
run_feed CCC_AGENT_PROVIDER=danso >/dev/null 2>&1
ok "second tick re-ingests nothing" "jq -e '.ingested == 0' '$STATUS' >/dev/null"
ok "second tick still sees both job files" "jq -e '.sources == 2' '$STATUS' >/dev/null"

# --- 3. provider drift is reported, not silent ------------------------------
# This is the #1698 failure mode: the lane runs, exits 0, ticks fresh — and is
# the wrong lane. The tick must say so and the run must complain on stderr.
run_feed CCC_AGENT_PROVIDER=piri >/dev/null 2>"$TMP/drift.err"
ok "drift flagged in the tick" \
  "jq -e '.feed_provider_mismatch == true and .feed_provider == \"piri\" and .feed == \"danso\"' '$STATUS' >/dev/null"
ok "drift explained on stderr" "grep -q 'provider drift' '$TMP/drift.err'"

# --- 4. provider resolved from the bridge .env when env is unset ------------
# The value is deliberately read at runtime rather than pinned into cron: a
# frozen copy would agree with the lane forever and never surface the drift.
BOT_DATA="$TMP/botdata"; mkdir -p "$BOT_DATA"
printf 'CCC_AGENT_PROVIDER=codex  # switched\n' > "$BOT_DATA/.env"
run_feed BOT_DATA_DIR="$BOT_DATA" >/dev/null 2>&1
ok "provider read from the bridge .env" \
  "jq -e '.feed_provider == \"codex\" and .feed_provider_mismatch == true' '$STATUS' >/dev/null"

# An unknown provider is not evidence of agreement — it must not be reported as
# a match, and must not be reported as drift either.
rm -f "$BOT_DATA/.env"
run_feed BOT_DATA_DIR="$BOT_DATA" >/dev/null 2>&1
ok "unknown provider omitted rather than guessed" \
  "! jq -e 'has(\"feed_provider\") or has(\"feed_provider_mismatch\")' '$STATUS' >/dev/null"

# --- 5. an absent journal is loud, and still a liveness tick ----------------
# A Danso node with bridge distill off (the shipped default) has no journal at
# all. Exiting silently would let ccc-doctor age the lane into ingest-tick-stale
# and bury the real cause.
MISSING="$TMP/nunchi-missing"; mkdir -p "$MISSING"
env CCC_STATE_DIR="$STATE" NUNCHI_HOME="$MISSING" NUNCHI_DB="$TMP/m.db" \
  NUNCHI_SNAPSHOT="$TMP/m.md" CCC_BRIDGE_DISTILL_JOURNAL="$TMP/nope" \
  CCC_AGENT_PROVIDER=danso bash "$FEED" >/dev/null 2>"$TMP/missing.err"
rc5=$?
ok "absent journal still exits 0" "[ $rc5 = 0 ]"
ok "absent journal reported as a skip reason" \
  "jq -e '.skipped == \"distill-journal-missing\" and .feed == \"danso\"' '$MISSING/ingest.status.json' >/dev/null"
ok "absent journal names the fix on stderr" \
  "grep -q 'CCC_MEMORY_DISTILL_PROVIDER=danso' '$TMP/missing.err'"

# --- 6. disabled mode is a hard no-op --------------------------------------
OFFHOME="$TMP/nunchi-off"; mkdir -p "$OFFHOME"
env CCC_STATE_DIR="$STATE" CCC_NUNCHI_MODE=off NUNCHI_HOME="$OFFHOME" \
  CCC_BRIDGE_DISTILL_JOURNAL="$JOURNAL" bash "$FEED" >/dev/null 2>&1
ok "mode=off writes no tick" "[ ! -e '$OFFHOME/ingest.status.json' ]"

# --- 7. every lane shares one tick writer ----------------------------------
# The four feeds used to each own a copy of the printf, which is how the claude
# lane ended up without a `feed` key and the drift flag reached none of them.
for lane in ingest-cron.sh codex-feed.sh piri-feed.sh danso-feed.sh; do
  ok "$lane uses the shared tick writer" \
    "grep -q 'nunchi_write_status' '$ROOT/claude/hooks/nunchi/$lane'"
  ok "$lane no longer hand-rolls the tick" \
    "! grep -q 'printf .{\"schema\"' '$ROOT/claude/hooks/nunchi/$lane'"
done
ok "the shared writer owns the schema string" \
  "grep -q 'ccc.nunchi.ingest.v1' '$LIB'"

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
