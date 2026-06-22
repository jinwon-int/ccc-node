#!/usr/bin/env bash
# Tests for agent-cron store/list/due slices — no execution, no push, no scheduler.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CMD="$ROOT/scripts/agent-cron.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

STORE="$TMP/tasks.json"
cat > "$STORE" <<'JSON'
{
  "version": 1,
  "tasks": [
    {
      "id": "daily-wiki-prefetch",
      "name": "Daily wiki prefetch",
      "schedule": "15 9 * * *",
      "prompt": "Summarize stale wiki cache entries.",
      "enabled": true,
      "allowedTools": ["Read", "Grep", "Glob"],
      "permissionMode": "dontAsk",
      "notify": "telegram-owner",
      "attachMemory": ["/root/.claude/memories/MEMORY.md"],
      "attachSkills": ["wiki-record"],
      "redactProfile": "default",
      "timezone": "UTC",
      "catchUpPolicy": "skip",
      "maxCatchup": 1,
      "lockTimeoutSec": 0,
      "lastRunAt": null,
      "lastStatus": "never",
      "lastRunId": null
    }
  ]
}
JSON

out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" list)"; rc=$?
ok "list valid store exits 0" '[ "$rc" = 0 ]'
ok "list renders task id" 'grep -q "daily-wiki-prefetch" <<<"$out"'
ok "list renders owner notify policy" 'grep -q "telegram-owner" <<<"$out"'
ok "list renders catch-up policy" 'grep -q "skip" <<<"$out"'

out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" list --json)"; rc=$?
ok "list --json exits 0" '[ "$rc" = 0 ]'
ok "list --json is valid JSON" 'jq -e ".version == 1 and (.tasks|length)==1 and .tasks[0].catchUpPolicy == \"skip\"" <<<"$out" >/dev/null'
ok "list --json preserves redact profile" 'jq -e ".tasks[0].redactProfile == \"default\"" <<<"$out" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$TMP/missing.json" bash "$CMD" list)"; rc=$?
ok "missing store lists empty safely" '[ "$rc" = 0 ] && grep -q "No agent-cron tasks" <<<"$out"'

BAD="$TMP/bad.json"
cat > "$BAD" <<'JSON'
{"version":1,"tasks":[{"id":"dup","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none"},{"id":"dup","schedule":"* * * * *","prompt":"b","enabled":true,"notify":"none"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD" bash "$CMD" validate 2>&1)"; rc=$?
ok "duplicate ids fail validation" '[ "$rc" = 1 ] && grep -q "duplicate task id" <<<"$out"'

BAD_NOTIFY="$TMP/bad-notify.json"
cat > "$BAD_NOTIFY" <<'JSON'
{"version":1,"tasks":[{"id":"bad","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"public-channel"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_NOTIFY" bash "$CMD" validate 2>&1)"; rc=$?
ok "unsupported notify target fails closed" '[ "$rc" = 1 ] && grep -q "notify" <<<"$out"'

BAD_REDACT="$TMP/bad-redact.json"
cat > "$BAD_REDACT" <<'JSON'
{"version":1,"tasks":[{"id":"bad-redact","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","redactProfile":42}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_REDACT" bash "$CMD" validate 2>&1)"; rc=$?
ok "non-string redactProfile fails validation" '[ "$rc" = 1 ] && grep -q "redactProfile" <<<"$out"'

BAD_TZ="$TMP/bad-tz.json"
cat > "$BAD_TZ" <<'JSON'
{"version":1,"tasks":[{"id":"bad-tz","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","timezone":"Asia/Seoul"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_TZ" bash "$CMD" validate 2>&1)"; rc=$?
ok "non-UTC timezone fails closed in this slice" '[ "$rc" = 1 ] && grep -q "timezone" <<<"$out"'

DUE="$TMP/due.json"
cat > "$DUE" <<'JSON'
{
  "version": 1,
  "tasks": [
    {"id":"hourly-skip","schedule":"@hourly","prompt":"a","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","catchUpPolicy":"skip"},
    {"id":"hourly-all","schedule":"0 * * * *","prompt":"b","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","catchUpPolicy":"all","maxCatchup":2},
    {"id":"daily-idle","schedule":"0 0 * * *","prompt":"c","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z"},
    {"id":"disabled","schedule":"* * * * *","prompt":"d","enabled":false,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z"}
  ]
}
JSON
before="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
out="$(CCC_AGENT_CRON_STORE="$DUE" bash "$CMD" due --json --at 2026-01-01T02:05:00Z)"; rc=$?
after="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
ok "due --json exits 0" '[ "$rc" = 0 ]'
ok "due --json is valid dry-run JSON" 'jq -e ".ok == true and .mode == \"dry-run-read-only\"" <<<"$out" >/dev/null'
ok "due identifies hourly skip task" 'jq -e ".tasks[] | select(.id == \"hourly-skip\" and .due == true and .dueCount == 1 and .missedRuns == 1 and .scheduledAt == \"2026-01-01T02:00:00Z\")" <<<"$out" >/dev/null'
ok "due honours all catch-up max" 'jq -e ".tasks[] | select(.id == \"hourly-all\" and .due == true and .dueCount == 2 and .missedRuns == 0)" <<<"$out" >/dev/null'
ok "due leaves idle daily task idle" 'jq -e ".tasks[] | select(.id == \"daily-idle\" and .due == false and .nextDueAt == \"2026-01-02T00:00:00Z\")" <<<"$out" >/dev/null'
ok "due leaves disabled task disabled" 'jq -e ".tasks[] | select(.id == \"disabled\" and .status == \"disabled\" and .due == false)" <<<"$out" >/dev/null'
ok "due made no filesystem changes" '[ "$before" = "$after" ]'

DOM_DOW="$TMP/dom-dow.json"
cat > "$DOM_DOW" <<'JSON'
{"version":1,"tasks":[{"id":"friday-or-thirteenth","schedule":"0 0 13 * 5","prompt":"a","enabled":true,"notify":"none","lastRunAt":"2026-02-19T00:00:00Z"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$DOM_DOW" bash "$CMD" due --json --at 2026-02-20T00:00:00Z)"; rc=$?
ok "due uses standard cron DOM/DOW OR semantics" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"friday-or-thirteenth\" and .due == true and .scheduledAt == \"2026-02-20T00:00:00Z\")" <<<"$out" >/dev/null'

TRUNC="$TMP/truncated.json"
cat > "$TRUNC" <<'JSON'
{"version":1,"tasks":[{"id":"dense","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","lastRunAt":null,"catchUpPolicy":"all","maxCatchup":100}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$TRUNC" bash "$CMD" due --json --at 2026-01-01T00:00:00Z)"; rc=$?
ok "due reports when missed-run scan is truncated" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"dense\" and .missedRunsTruncated == true and .occurrenceScanLimit == 1000)" <<<"$out" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$DUE" bash "$CMD" due --json --at not-a-date 2>&1)"; rc=$?
ok "invalid --at fails closed without traceback" '[ "$rc" = 1 ] && grep -q -- "--at" <<<"$out" && ! grep -q "Traceback" <<<"$out"'

out="$(CCC_AGENT_CRON_STORE="$DUE" bash "$CMD" due --at 2026-01-01T02:05:00Z)"; rc=$?
ok "due table exits 0" '[ "$rc" = 0 ] && grep -q "dry-run/read-only" <<<"$out" && grep -q "hourly-skip" <<<"$out"'

LOCK_STORE="$TMP/lock-store/tasks.json"
mkdir -p "$(dirname "$LOCK_STORE")"
cat > "$LOCK_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"locky","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" lock locky --action acquire --run-id run-1 --scheduled-at 2026-01-01T00:01:00Z --at 2026-01-01T00:01:00Z --json)"; rc=$?
ok "lock acquire creates lock atomically" '[ "$rc" = 0 ] && jq -e ".ok == true and .lockState == \"acquired\" and .runId == \"run-1\"" <<<"$out" >/dev/null && jq -e ".runId == \"run-1\"" "$TMP/lock-store/locks/locky.lock" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" lock locky --action acquire --run-id run-2 --at 2026-01-01T00:01:10Z --json 2>&1)"; rc=$?
ok "lock acquire fails while held" '[ "$rc" = 1 ] && jq -e ".ok == false and .lockState == \"held\" and .holder.runId == \"run-1\"" <<<"$out" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" due --json --at 2026-01-01T00:01:10Z)"; rc=$?
ok "due reports held lock" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"locky\" and .lockState == \"held\" and .status == \"locked\")" <<<"$out" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" lock locky --action release --run-id run-2 --json 2>&1)"; rc=$?
ok "lock release refuses runId mismatch" '[ "$rc" = 1 ] && jq -e ".ok == false and .lockState == \"release-mismatch\"" <<<"$out" >/dev/null && jq -e ".runId == \"run-1\"" "$TMP/lock-store/locks/locky.lock" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" lock locky --action release --run-id run-1 --json)"; rc=$?
ok "lock release removes owned lock" '[ "$rc" = 0 ] && jq -e ".ok == true and .lockState == \"released\"" <<<"$out" >/dev/null && [ ! -e "$TMP/lock-store/locks/locky.lock" ]'

mkdir -p "$TMP/lock-store/locks"
cat > "$TMP/lock-store/locks/locky.lock" <<'JSON'
{"taskId":"locky","runId":"old-run","pid":999999,"host":"test","bootId":"old-boot","acquiredAt":"2026-01-01T00:00:00Z","scheduledAt":"2026-01-01T00:00:00Z"}
JSON
out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" due --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "due reports stale lock using lockTimeoutSec" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"locky\" and .lockState == \"stale\" and .status == \"stale-lock\")" <<<"$out" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$LOCK_STORE" bash "$CMD" lock locky --action acquire --run-id run-3 --at 2026-01-01T00:02:00Z --json)"; rc=$?
ok "lock acquire reclaims stale lock" '[ "$rc" = 0 ] && jq -e ".ok == true and .lockState == \"acquired\" and .reclaimedStale == true and .runId == \"run-3\"" <<<"$out" >/dev/null && jq -e ".runId == \"run-3\"" "$TMP/lock-store/locks/locky.lock" >/dev/null'

BAD_SCHED="$TMP/bad-schedule.json"
cat > "$BAD_SCHED" <<'JSON'
{"version":1,"tasks":[{"id":"bad-sched","schedule":"0 0 0 0 0 0","prompt":"a","enabled":true,"notify":"none"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_SCHED" bash "$CMD" due --json --at 2026-01-01T00:00:00Z 2>&1)"; rc=$?
ok "due fails closed on unsupported schedule" '[ "$rc" = 1 ] && grep -q "unsupported\|5-field" <<<"$out"'

RUN_STORE="$TMP/run-store/tasks.json"
mkdir -p "$(dirname "$RUN_STORE")"
cat > "$RUN_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"runny","schedule":"* * * * *","prompt":"Summarize safely","enabled":true,"notify":"telegram-owner","allowedTools":["Read","Grep"],"permissionMode":"dontAsk","attachMemory":["MEMORY.md"],"attachSkills":["wiki-record"],"redactProfile":"owner","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60}]}
JSON
before="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
out="$(CCC_AGENT_CRON_STORE="$RUN_STORE" CCC_HEADLESS_CMD="$TMP/fake-headless.sh" bash "$CMD" run runny --dry-run --json --at 2026-01-01T00:01:00Z)"; rc=$?
after="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
ok "run --dry-run emits deterministic execution plan JSON" '[ "$rc" = 0 ] && jq -e ".ok == true and .mode == \"run-dry-run-read-only\" and .taskId == \"runny\" and .due == true and .scheduledAt == \"2026-01-01T00:01:00Z\"" <<<"$out" >/dev/null'
ok "run --dry-run includes headless and task policy" '[ "$rc" = 0 ] && jq -e ".headless.command == \"$TMP/fake-headless.sh\" and .headless.permissionMode == \"dontAsk\" and (.headless.allowedTools == [\"Read\",\"Grep\"]) and (.headless.attachMemory == [\"MEMORY.md\"]) and (.headless.attachSkills == [\"wiki-record\"])" <<<"$out" >/dev/null'
ok "run --dry-run previews owner notification without sending" '[ "$rc" = 0 ] && jq -e ".notification.policy == \"telegram-owner\" and .notification.delivery == \"preview-only\" and .notification.redactProfile == \"owner\"" <<<"$out" >/dev/null'
ok "run --dry-run declares no mutations" '[ "$rc" = 0 ] && jq -e ".mutations.lockAcquire == false and .mutations.taskStoreWrite == false and .mutations.historyAppend == false and .mutations.pushSpoolWrite == false and .mutations.schedulerInstall == false" <<<"$out" >/dev/null'
ok "run --dry-run made no filesystem changes" '[ "$before" = "$after" ]'

before="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
out="$(CCC_AGENT_CRON_STORE="$RUN_STORE" bash "$CMD" run runny --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
after="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
ok "run without --dry-run is not implemented in current slice" '[ "$rc" = 2 ] && grep -q "not implemented" <<<"$out"'
ok "run without --dry-run made no filesystem changes" '[ "$before" = "$after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
