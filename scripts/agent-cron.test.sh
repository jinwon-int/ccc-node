#!/usr/bin/env bash
# Tests for agent-cron store/list/due/scheduler-dry-run slices.
# No timer install, direct provider send, live task execution, or remote mutation.
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

PY_CMD="$ROOT/scripts/agent_cron.py"
out="$(CCC_AGENT_CRON_STORE="$STORE" python3 "$PY_CMD" list --json 2>&1)"; rc=$?
ok "python agent-cron entrypoint exists and lists JSON" '[ "$rc" = 0 ] && jq -e ".version == 1 and (.tasks|length)==1 and .tasks[0].id == \"daily-wiki-prefetch\"" <<<"$out" >/dev/null'
ok "shell wrapper delegates to python entrypoint" 'grep -q "agent_cron.py" "$CMD"'

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

BAD_RETRY="$TMP/bad-retry.json"
cat > "$BAD_RETRY" <<'JSON'
{"version":1,"tasks":[{"id":"bad-retry","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","retryPolicy":{"maxAttempts":0}}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_RETRY" bash "$CMD" validate 2>&1)"; rc=$?
ok "invalid retryPolicy fails validation" '[ "$rc" = 1 ] && grep -q "retryPolicy.maxAttempts" <<<"$out"'

IANA_TZ="$TMP/iana-tz.json"
cat > "$IANA_TZ" <<'JSON'
{"version":1,"tasks":[{"id":"kst-task","schedule":"0 9 * * *","prompt":"a","enabled":true,"notify":"none","timezone":"Asia/Seoul"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$IANA_TZ" bash "$CMD" validate 2>&1)"; rc=$?
ok "IANA timezone validates" '[ "$rc" = 0 ]'
out="$(CCC_AGENT_CRON_STORE="$IANA_TZ" bash "$CMD" due --at 2026-08-02T00:00:00Z --json 2>&1)"; rc=$?
ok "KST 09:00 cron is due at 00:00 UTC" '[ "$rc" = 0 ] && grep -q '"'"'"due": true'"'"' <<<"$out"'

BAD_TZ="$TMP/bad-tz.json"
cat > "$BAD_TZ" <<'JSON'
{"version":1,"tasks":[{"id":"bad-tz","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","timezone":"Mars/OlympusMons"}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_TZ" bash "$CMD" due --json 2>&1)"; rc=$?
ok "unknown timezone fails closed as invalid-schedule" 'grep -q "invalid-schedule" <<<"$out" && grep -q "unknown timezone" <<<"$out"'

KINDS="$TMP/kinds.json"
cat > "$KINDS" <<'JSON'
{"version":1,"tasks":[
  {"id":"interval-task","schedule":"every 30m","prompt":"a","enabled":true,"notify":"none"},
  {"id":"once-task","schedule":"at 2026-08-01T09:00:00Z","prompt":"a","enabled":true,"notify":"none"},
  {"id":"once-done","schedule":"at 2026-08-01T09:00:00Z","prompt":"a","enabled":true,"notify":"none","lastRunAt":"2026-08-01T09:00:00Z"}
]}
JSON
out="$(CCC_AGENT_CRON_STORE="$KINDS" bash "$CMD" due --at 2026-08-01T09:05:00Z --json 2>&1)"; rc=$?
ok "interval task never run is due" '[ "$rc" = 0 ] && python3 -c "
import json,sys
doc=json.loads(sys.argv[1])
rows={t[\"id\"]:t for t in doc[\"tasks\"]}
assert rows[\"interval-task\"][\"due\"] is True, rows[\"interval-task\"]
assert rows[\"interval-task\"][\"scheduleKind\"]==\"interval\"
assert rows[\"once-task\"][\"due\"] is True
assert rows[\"once-task\"][\"scheduleKind\"]==\"once\"
assert rows[\"once-done\"][\"due\"] is False
assert rows[\"once-done\"][\"nextDueAt\"] is None
" "$out"'

DUE="$TMP/due.json"
cat > "$DUE" <<'JSON'
{
  "version": 1,
  "tasks": [
    {"id":"hourly-skip","schedule":"@hourly","prompt":"a","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","catchUpPolicy":"skip"},
    {"id":"hourly-all","schedule":"0 * * * *","prompt":"b","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","catchUpPolicy":"all","maxCatchup":2},
    {"id":"daily-idle","schedule":"0 0 * * *","prompt":"c","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z"},
    {"id":"disabled","schedule":"* * * * *","prompt":"d","enabled":false,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z"},
    {"id":"future-once","schedule":"27 2 22 7 *","prompt":"e","enabled":true,"notify":"none","notBefore":"2026-07-22T02:27:00Z","maxRuns":1}
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
ok "notBefore prevents an annual one-time task from catching up early" 'jq -e ".tasks[] | select(.id == \"future-once\" and .status == \"not-before\" and .due == false and .nextDueAt == \"2026-07-22T02:27:00Z\")" <<<"$out" >/dev/null'
ok "due made no filesystem changes" '[ "$before" = "$after" ]'

out="$(CCC_AGENT_CRON_STORE="$DUE" bash "$CMD" due --json --at 2026-07-22T02:27:00Z)"; rc=$?
ok "notBefore task becomes due at its bounded first occurrence" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"future-once\" and .due == true and .scheduledAt == \"2026-07-22T02:27:00Z\")" <<<"$out" >/dev/null'

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

RETRY_DUE="$TMP/retry-due.json"
cat > "$RETRY_DUE" <<'JSON'
{"version":1,"tasks":[{"id":"retry-wait","schedule":"0 0 * * *","prompt":"a","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","retryPolicy":{"maxAttempts":3,"backoffSec":60,"backoffMultiplier":2,"maxBackoffSec":300},"retryState":{"scheduledAt":"2026-01-01T00:00:00Z","attempt":1,"retryEligibleAt":"2026-01-01T00:10:00Z","lastStatus":"failed","lastRunId":"r1"}},{"id":"retry-ready","schedule":"0 0 * * *","prompt":"b","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","retryPolicy":{"maxAttempts":3,"backoffSec":60},"retryState":{"scheduledAt":"2026-01-01T00:00:00Z","attempt":1,"retryEligibleAt":"2026-01-01T00:02:00Z","lastStatus":"failed","lastRunId":"r1"}}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$RETRY_DUE" bash "$CMD" due --json --at 2026-01-01T00:05:00Z)"; rc=$?
ok "due shows retry wait without execution" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"retry-wait\" and .due == false and .status == \"retry-wait\" and .retryEligibleAt == \"2026-01-01T00:10:00Z\")" <<<"$out" >/dev/null'
ok "due exposes eligible retry as retry-due" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"retry-ready\" and .due == true and .status == \"retry-due\" and .scheduledAt == \"2026-01-01T00:00:00Z\" and .retryAttempt == 2)" <<<"$out" >/dev/null'

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

SCHED_STORE="$TMP/scheduler-store/tasks.json"
mkdir -p "$(dirname "$SCHED_STORE")"
cat > "$SCHED_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"sched-due","schedule":"* * * * *","prompt":"Run me","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"sched-retry","schedule":"0 0 * * *","prompt":"Retry me","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","retryPolicy":{"maxAttempts":3,"backoffSec":60},"retryState":{"scheduledAt":"2026-01-01T00:00:00Z","attempt":1,"retryEligibleAt":"2026-01-01T00:02:00Z","lastStatus":"failed","lastRunId":"r1"}},{"id":"sched-disabled","schedule":"* * * * *","prompt":"No","enabled":false,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z"},{"id":"sched-locked","schedule":"* * * * *","prompt":"Locked","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":600}]}
JSON
mkdir -p "$TMP/scheduler-store/locks"
cat > "$TMP/scheduler-store/locks/sched-locked.lock" <<'JSON'
{"taskId":"sched-locked","runId":"held","pid":999999,"host":"test","bootId":"","acquiredAt":"2026-01-01T00:04:00Z","scheduledAt":"2026-01-01T00:04:00Z"}
JSON
before="$(find "$TMP/scheduler-store" -type f -printf '%P %s %T@\n' | sort)"
out="$(CCC_AGENT_CRON_STORE="$SCHED_STORE" bash "$CMD" scheduler --dry-run --json --at 2026-01-01T00:05:00Z)"; rc=$?
after="$(find "$TMP/scheduler-store" -type f -printf '%P %s %T@\n' | sort)"
ok "scheduler --dry-run emits read-only plan" '[ "$rc" = 0 ] && jq -e ".ok == true and .mode == \"scheduler-dry-run-read-only\" and .mutations.lockAcquire == false and .mutations.taskStoreWrite == false and .mutations.headlessExecute == false" <<<"$out" >/dev/null'
ok "scheduler --dry-run plans due and retry-due actions" 'jq -e ".actions[] | select(.taskId == \"sched-due\" and .action == \"would-run\" and .status == \"due\")" <<<"$out" >/dev/null && jq -e ".actions[] | select(.taskId == \"sched-retry\" and .action == \"would-run\" and .status == \"retry-due\" and .retryAttempt == 2)" <<<"$out" >/dev/null'
ok "scheduler --dry-run skips disabled and locked tasks" 'jq -e ".actions[] | select(.taskId == \"sched-disabled\" and .action == \"skip\" and .reason == \"disabled\")" <<<"$out" >/dev/null && jq -e ".actions[] | select(.taskId == \"sched-locked\" and .action == \"skip\" and .reason == \"locked\")" <<<"$out" >/dev/null'
ok "scheduler --dry-run made no filesystem changes" '[ "$before" = "$after" ]'
out="$(CCC_AGENT_CRON_STORE="$SCHED_STORE" bash "$CMD" scheduler --json --at 2026-01-01T00:05:00Z 2>&1)"; rc=$?
ok "scheduler without mode is blocked" '[ "$rc" = 2 ] && grep -q "requires --dry-run or --execute" <<<"$out"'

INSTALLER="$ROOT/scripts/install-agent-cron-systemd.sh"
FAKE_SYSTEMCTL="$TMP/fake-systemctl.sh"
cat > "$FAKE_SYSTEMCTL" <<'SH'
#!/usr/bin/env bash
printf '%s
' "$*" >> "$FAKE_SYSTEMCTL_LOG"
SH
chmod +x "$FAKE_SYSTEMCTL"
export FAKE_SYSTEMCTL_LOG="$TMP/systemctl.log"
SYSTEMD_DIR="$TMP/systemd"
before="$(find "$TMP" -type f -printf '%P %s %T@
' | sort)"
out="$(CCC_SYSTEMD_DIR="$SYSTEMD_DIR" CCC_SYSTEMCTL="$FAKE_SYSTEMCTL" bash "$INSTALLER" --dry-run --service-name ccc-agent-cron-test 2>&1)"; rc=$?
after="$(find "$TMP" -type f -printf '%P %s %T@
' | sort)"
ok "systemd installer dry-run writes nothing" '[ "$rc" = 0 ] && grep -q "dry-run: would write" <<<"$out" && [ "$before" = "$after" ]'
out="$(CCC_SYSTEMD_DIR="$SYSTEMD_DIR" CCC_SYSTEMCTL="$FAKE_SYSTEMCTL" bash "$INSTALLER" --apply --service-name ccc-agent-cron-test --store "$SCHED_STORE" --headless "$TMP/headless.sh" --spool "$TMP/spool" 2>&1)"; rc=$?
ok "systemd installer apply writes service and timer" '[ "$rc" = 0 ] && [ -f "$SYSTEMD_DIR/ccc-agent-cron-test.service" ] && [ -f "$SYSTEMD_DIR/ccc-agent-cron-test.timer" ] && grep -q "scheduler --execute --json" "$SYSTEMD_DIR/ccc-agent-cron-test.service" && grep -q "Persistent=true" "$SYSTEMD_DIR/ccc-agent-cron-test.timer"'
ok "systemd installer apply reloads/enables/restarts timer" 'grep -q "daemon-reload" "$FAKE_SYSTEMCTL_LOG" && grep -q "enable --now ccc-agent-cron-test.timer" "$FAKE_SYSTEMCTL_LOG" && grep -q "restart ccc-agent-cron-test.timer" "$FAKE_SYSTEMCTL_LOG"'

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


EXEC_STORE="$TMP/exec-store/tasks.json"
mkdir -p "$(dirname "$EXEC_STORE")"
cat > "$EXEC_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"exec-success","schedule":"* * * * *","prompt":"Run safely","enabled":true,"notify":"none","allowedTools":["Read","Grep"],"permissionMode":"dontAsk","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-fail","schedule":"* * * * *","prompt":"Fail safely","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-disabled","schedule":"* * * * *","prompt":"Disabled","enabled":false,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-not-due","schedule":"0 0 * * *","prompt":"Not due","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-notify","schedule":"* * * * *","prompt":"Notify safely","enabled":true,"notify":"telegram-owner","redactProfile":"owner","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-notify-fail","schedule":"* * * * *","prompt":"Notify Fail secret","enabled":true,"notify":"telegram-owner","redactProfile":"owner","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-history","schedule":"* * * * *","prompt":"History safely","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60,"maxRunHistory":3},{"id":"exec-history-prune","schedule":"* * * * *","prompt":"History prune","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60,"maxRunHistory":2,"runHistory":[{"runId":"old-1","scheduledAt":"2026-01-01T00:00:00Z","startedAt":"2026-01-01T00:00:00Z","finishedAt":"2026-01-01T00:00:00Z","status":"success","exitCode":0,"attempt":1,"notifyState":"none"},{"runId":"old-2","scheduledAt":"2026-01-01T00:01:00Z","startedAt":"2026-01-01T00:01:00Z","finishedAt":"2026-01-01T00:01:00Z","status":"success","exitCode":0,"attempt":1,"notifyState":"none"}]},{"id":"exec-retry","schedule":"* * * * *","prompt":"Fail retry","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60,"retryPolicy":{"maxAttempts":3,"backoffSec":60,"backoffMultiplier":2,"maxBackoffSec":300}},{"id":"exec-retry-success","schedule":"0 0 * * *","prompt":"Run retry success","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60,"retryPolicy":{"maxAttempts":3,"backoffSec":60},"retryState":{"scheduledAt":"2026-01-01T00:00:00Z","attempt":1,"retryEligibleAt":"2026-01-01T00:02:00Z","lastStatus":"failed","lastRunId":"r1"}},{"id":"exec-once","schedule":"* * * * *","prompt":"Once safely","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","maxRuns":1},{"id":"exec-once-fail","schedule":"* * * * *","prompt":"Once Fail","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","maxRuns":1,"retryPolicy":{"maxAttempts":3,"backoffSec":60}}]}
JSON
FAKE_HEADLESS="$TMP/fake-headless-exec.sh"
cat > "$FAKE_HEADLESS" <<'SH'
#!/usr/bin/env bash
set -u
printf 'prompt=%s
allowed=%s
perm=%s
model=%s
' "$1" "${CCC_ALLOWED_TOOLS:-}" "${CCC_PERMISSION_MODE:-}" "${CCC_MODEL:-}" >> "$FAKE_HEADLESS_LOG"
case "$1" in
  *Fail*) echo "fake failure" >&2; exit 7 ;;
  *) echo "fake result for $1 token=abcdefghijklmnopqrstuvwxyz1234567890" ;;
esac
SH
chmod +x "$FAKE_HEADLESS"
export FAKE_HEADLESS_LOG="$TMP/fake-headless.log"

SCHED_EXEC_STORE="$TMP/scheduler-exec-store/tasks.json"
mkdir -p "$(dirname "$SCHED_EXEC_STORE")"
cat > "$SCHED_EXEC_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"sched-exec-success","schedule":"* * * * *","prompt":"Scheduler success","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"sched-exec-fail","schedule":"* * * * *","prompt":"Scheduler Fail","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$SCHED_EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" scheduler --execute --json --at 2026-01-01T00:01:00Z --max-runs 2)"; rc=$?
ok "scheduler --execute runs due tasks through run path" '[ "$rc" = 0 ] && jq -e ".mode == \"scheduler-execute-one-shot\" and .executedActions == 2 and .mutations.lockAcquire == true and .mutations.taskStoreWrite == true and .mutations.headlessExecute == true and .mutations.historyAppend == true" <<<"$out" >/dev/null'
ok "scheduler --execute records success and failure without keeping locks" 'jq -e ".tasks[] | select(.id == \"sched-exec-success\" and .lastStatus == \"success\" and (.runHistory|length)==1)" "$SCHED_EXEC_STORE" >/dev/null && jq -e ".tasks[] | select(.id == \"sched-exec-fail\" and .lastStatus == \"failed\" and (.runHistory|length)==1)" "$SCHED_EXEC_STORE" >/dev/null && [ ! -e "$TMP/scheduler-exec-store/locks/sched-exec-success.lock" ] && [ ! -e "$TMP/scheduler-exec-store/locks/sched-exec-fail.lock" ]'

ONESHOT_STORE="$TMP/oneshot-store/tasks.json"
mkdir -p "$(dirname "$ONESHOT_STORE")"
cat > "$ONESHOT_STORE" <<'JSON'
{"version":1,"tasks":[
  {"id":"oneshot-run","schedule":"at 2026-01-01T00:01:00Z","prompt":"Run once","enabled":true,"notify":"none"},
  {"id":"oneshot-keep","schedule":"at 2026-01-01T00:01:00Z","prompt":"Run once kept","enabled":true,"notify":"none","keepAfterRun":true}
]}
JSON
out="$(CCC_AGENT_CRON_STORE="$ONESHOT_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run oneshot-run --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "one-shot run succeeds and auto-disables" '[ "$rc" = 0 ] && jq -e ".ok == true and .oneShotDisabled == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"oneshot-run\" and .enabled == false and .lastStatus == \"success\")" "$ONESHOT_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$ONESHOT_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run oneshot-keep --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "keepAfterRun one-shot stays enabled" '[ "$rc" = 0 ] && jq -e ".oneShotDisabled == false" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"oneshot-keep\" and .enabled == true)" "$ONESHOT_STORE" >/dev/null'

CRUD_STORE="$TMP/crud-store/tasks.json"
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" add daily-report --schedule "0 9 * * *" --prompt "Daily report" --timezone Asia/Seoul --notify telegram-owner-on-failure --allowed-tools Read,Grep --not-before 2026-08-01T00:00:00Z --max-runs 1 --json)"; rc=$?
ok "add creates a bounded task in a fresh store" '[ "$rc" = 0 ] && jq -e ".ok == true and .mutations.taskStoreWrite == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"daily-report\" and .timezone == \"Asia/Seoul\" and .notify == \"telegram-owner-on-failure\" and .notBefore == \"2026-08-01T00:00:00Z\" and .maxRuns == 1 and .enabled == true)" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" add daily-report --schedule "@daily" --prompt "dup" --json 2>&1)"; rc=$?
ok "add rejects duplicate id" '[ "$rc" = 1 ] && grep -q "already exists" <<<"$out"'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" add bad-sched --schedule "not a schedule" --prompt "x" --json 2>&1)"; rc=$?
ok "add rejects invalid schedule without writing" '[ "$rc" = 2 ] && ! jq -e ".tasks[] | select(.id == \"bad-sched\")" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" add bad-bound --schedule "@daily" --prompt "x" --not-before never --json 2>&1)"; rc=$?
ok "add rejects invalid not-before without writing" '[ "$rc" = 2 ] && ! jq -e ".tasks[] | select(.id == \"bad-bound\")" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" add watchdog --schedule "every 30m" --prompt "disk watchdog" --argv sh --argv -c --argv "df -h" --timeout-sec 30 --json)"; rc=$?
ok "add creates a command-payload task" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"watchdog\") | .payload.kind == \"command\" and .payload.argv == [\"sh\",\"-c\",\"df -h\"] and .payload.timeoutSec == 30" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" disable watchdog --json)"; rc=$?
ok "disable toggles enabled off" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"watchdog\" and .enabled == false)" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" enable watchdog --json)"; rc=$?
ok "enable toggles enabled on" '[ "$rc" = 0 ] && jq -e ".changed == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"watchdog\" and .enabled == true)" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" edit watchdog --schedule "every 30m" --json)"; rc=$?
ok "edit updates schedule in place" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"watchdog\" and .schedule == \"every 30m\") | .payload.argv == [\"sh\",\"-c\",\"df -h\"]" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" edit watchdog --timeout-sec 45 --json)"; rc=$?
ok "edit merges payload fields preserving argv" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"watchdog\") | .payload.timeoutSec == 45 and .payload.kind == \"command\"" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" edit watchdog --schedule "not a schedule" --json 2>&1)"; rc=$?
ok "edit rejects invalid schedule without write" '[ "$rc" = 2 ] && jq -e ".tasks[] | select(.id == \"watchdog\" and .schedule == \"every 30m\")" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" edit watchdog --json 2>&1)"; rc=$?
ok "edit with no flags is rejected" '[ "$rc" = 2 ] && grep -q "at least one field" <<<"$out"'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" edit missing-task --name x --json 2>&1)"; rc=$?
ok "edit unknown id fails" '[ "$rc" = 1 ] && grep -q "not found" <<<"$out"'

out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" remove watchdog --json)"; rc=$?
ok "remove deletes the task" '[ "$rc" = 0 ] && ! jq -e ".tasks[] | select(.id == \"watchdog\")" "$CRUD_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$CRUD_STORE" bash "$CMD" remove missing-task --json 2>&1)"; rc=$?
ok "remove unknown id fails" '[ "$rc" = 1 ] && grep -q "not found" <<<"$out"'

NOTIFY_FAIL_STORE="$TMP/notify-fail-store/tasks.json"
mkdir -p "$(dirname "$NOTIFY_FAIL_STORE")"
cat > "$NOTIFY_FAIL_STORE" <<'JSON'
{"version":1,"tasks":[
  {"id":"nf-ok","schedule":"* * * * *","prompt":"Quiet when fine","enabled":true,"notify":"telegram-owner-on-failure","lastRunAt":"2026-01-01T00:00:00Z"},
  {"id":"nf-bad","schedule":"* * * * *","prompt":"Loud when Fail","enabled":true,"notify":"telegram-owner-on-failure","lastRunAt":"2026-01-01T00:00:00Z"}
]}
JSON
NF_SPOOL="$TMP/nf-spool"
out="$(CCC_AGENT_CRON_STORE="$NOTIFY_FAIL_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$NF_SPOOL" bash "$CMD" run nf-ok --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "on-failure notify skips successful runs" '[ "$rc" = 0 ] && jq -e ".notification.delivery == \"skipped-success\"" <<<"$out" >/dev/null && [ -z "$(ls -A "$NF_SPOOL" 2>/dev/null)" ]'
out="$(CCC_AGENT_CRON_STORE="$NOTIFY_FAIL_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$NF_SPOOL" bash "$CMD" run nf-bad --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "on-failure notify spools failed runs" '[ "$rc" = 1 ] && jq -e ".notification.delivery == \"spooled\"" <<<"$out" >/dev/null && [ -n "$(ls -A "$NF_SPOOL" 2>/dev/null)" ]'

PAYLOAD_STORE="$TMP/payload-store/tasks.json"
mkdir -p "$(dirname "$PAYLOAD_STORE")"
cat > "$PAYLOAD_STORE" <<'JSON'
{"version":1,"tasks":[
  {"id":"cmd-ok","schedule":"* * * * *","prompt":"echo watchdog","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","payload":{"kind":"command","argv":["sh","-c","echo cmd-stdout-ok"]}},
  {"id":"cmd-fail","schedule":"* * * * *","prompt":"failing command","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","payload":{"kind":"command","argv":["sh","-c","echo boom >&2; exit 3"]}},
  {"id":"cmd-slow","schedule":"* * * * *","prompt":"slow command","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","payload":{"kind":"command","argv":["sleep","5"],"timeoutSec":1}},
  {"id":"model-task","schedule":"* * * * *","prompt":"Model override run","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","payload":{"kind":"prompt","model":"claude-test-model"}}
]}
JSON
out="$(CCC_AGENT_CRON_STORE="$PAYLOAD_STORE" bash "$CMD" run cmd-ok --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "command payload runs argv without headless" '[ "$rc" = 0 ] && jq -e ".ok == true and .status == \"success\" and .headless.payloadKind == \"command\" and (.headless.stdout | contains(\"cmd-stdout-ok\"))" <<<"$out" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$PAYLOAD_STORE" bash "$CMD" run cmd-fail --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "command payload propagates failure exit code" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"failed\" and .headless.exitCode == 3 and (.headless.stderr | contains(\"boom\"))" <<<"$out" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$PAYLOAD_STORE" bash "$CMD" run cmd-slow --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "command payload times out with distinct status" '[ "$rc" = 1 ] && jq -e ".status == \"timeout\" and .headless.exitCode == 124 and .headless.timedOut == true" <<<"$out" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$PAYLOAD_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run model-task --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "prompt payload model override reaches headless env" '[ "$rc" = 0 ] && grep -q "model=claude-test-model" "$FAKE_HEADLESS_LOG"'

# --- #911: successExitCodes (watch-type exit 1 = findings, not failure) + lastExitCode ---
SUCCESS_EXIT_STORE="$TMP/success-exit-store/tasks.json"
mkdir -p "$(dirname "$SUCCESS_EXIT_STORE")"
cat > "$SUCCESS_EXIT_STORE" <<'JSON'
{"version":1,"tasks":[
{"id":"watch-findings","schedule":"* * * * *","prompt":"fleet watch","enabled":true,"notify":"none","payload":{"kind":"command","argv":["sh","-c","echo DOWN=1; exit 1"]},"successExitCodes":[0,1]},
{"id":"real-fail","schedule":"* * * * *","prompt":"real failure","enabled":true,"notify":"none","payload":{"kind":"command","argv":["sh","-c","echo boom; exit 2"]}}
]}
JSON
out="$(CCC_AGENT_CRON_STORE="$SUCCESS_EXIT_STORE" bash "$CMD" run watch-findings --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "watch task exit 1 with successExitCodes[0,1] is success-with-findings" '[ "$rc" = 0 ] && jq -e ".ok == true and .status == \"success\" and .headless.exitCode == 1" <<<"$out" >/dev/null'
ok "watch success records lastStatus success and no retryState" 'jq -e ".tasks[] | select(.id == \"watch-findings\" and .lastStatus == \"success\" and ((has(\"retryState\") | not) or .retryState == null))" "$SUCCESS_EXIT_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$SUCCESS_EXIT_STORE" bash "$CMD" run real-fail --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "real fail exit 2 with default codes is failed and records no retryState" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"failed\" and .headless.exitCode == 2" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"real-fail\" and .lastStatus == \"failed\" and ((has(\"retryState\") | not) or .retryState == null))" "$SUCCESS_EXIT_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$SUCCESS_EXIT_STORE" bash "$CMD" status --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "status exposes lastExitCode derived from runHistory" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"watch-findings\" and .lastExitCode == 1)" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"real-fail\" and .lastExitCode == 2)" <<<"$out" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$PAYLOAD_STORE" bash "$CMD" run cmd-ok --dry-run --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "dry-run previews command payload metadata" '[ "$rc" = 0 ] && jq -e ".headless.payloadKind == \"command\" and .headless.argvLen == 3" <<<"$out" >/dev/null'

BAD_PAYLOAD="$TMP/bad-payload.json"
cat > "$BAD_PAYLOAD" <<'JSON'
{"version":1,"tasks":[{"id":"bad-payload","schedule":"* * * * *","prompt":"a","enabled":true,"notify":"none","payload":{"kind":"command"}}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$BAD_PAYLOAD" bash "$CMD" validate 2>&1)"; rc=$?
ok "command payload without argv fails validation" '[ "$rc" = 1 ] && grep -q "argv is required" <<<"$out"'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-success --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "run executes due task with fake headless" '[ "$rc" = 0 ] && jq -e ".ok == true and .status == \"success\" and .mutations.lockAcquire == true and .mutations.taskStoreWrite == true and .mutations.headlessExecute == true" <<<"$out" >/dev/null'
ok "run passes prompt and policy to headless" 'grep -q "prompt=Run safely" "$FAKE_HEADLESS_LOG" && grep -q "allowed=Read,Grep" "$FAKE_HEADLESS_LOG" && grep -q "perm=dontAsk" "$FAKE_HEADLESS_LOG"'
ok "run records last successful state and releases lock" 'jq -e ".tasks[] | select(.id == \"exec-success\" and .lastRunAt == \"2026-01-01T00:01:00Z\" and .lastStatus == \"success\" and (.lastRunId|type == \"string\"))" "$EXEC_STORE" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-success.lock" ]'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-fail --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "run propagates headless failure" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"failed\" and .headless.exitCode == 7" <<<"$out" >/dev/null'
ok "run failure records state and releases lock" 'jq -e ".tasks[] | select(.id == \"exec-fail\" and .lastRunAt == \"2026-01-01T00:01:00Z\" and .lastStatus == \"failed\")" "$EXEC_STORE" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-fail.lock" ]'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-once --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "maxRuns disables a successful one-time task" '[ "$rc" = 0 ] && jq -e ".runLimit.reached == true and .runLimit.runCount == 1 and .runLimit.remainingRuns == 0" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"exec-once\" and .enabled == false and .runCount == 1)" "$EXEC_STORE" >/dev/null'
out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-once --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "disabled one-time task cannot recur" '[ "$rc" = 0 ] && jq -e ".status == \"run-limit-reached\" and .mutations.headlessExecute == false" <<<"$out" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-once-fail --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "maxRuns also bounds failures and cancels retry" '[ "$rc" = 1 ] && jq -e ".status == \"failed\" and .runLimit.reached == true and .retry.cancelledByRunLimit == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"exec-once-fail\" and .enabled == false and .runCount == 1 and (has(\"retryState\") | not))" "$EXEC_STORE" >/dev/null'

mkdir -p "$TMP/exec-store/locks"
boot_id="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
python3 - "$TMP/exec-store/locks/exec-success.lock" "$boot_id" <<'PY'
import json, sys
p=sys.argv[1]
boot=sys.argv[2]
open(p,'w',encoding='utf-8').write(json.dumps({"taskId":"exec-success","runId":"other-run","pid":999999,"host":"test","bootId":boot,"acquiredAt":"2026-01-01T00:02:00Z","scheduledAt":"2026-01-01T00:02:00Z"})+chr(10))
PY
out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-success --json --at 2026-01-01T00:02:00Z 2>&1)"; rc=$?
ok "run refuses held lock with nonzero exit" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"locked\"" <<<"$out" >/dev/null'
rm -f "$TMP/exec-store/locks/exec-success.lock"

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-disabled --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "run skips disabled task without lock" '[ "$rc" = 0 ] && jq -e ".ok == true and .status == \"disabled\" and .mutations.lockAcquire == false" <<<"$out" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-disabled.lock" ]'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-not-due --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "run skips not-due task without lock" '[ "$rc" = 0 ] && jq -e ".ok == true and .status == \"not-due\" and .mutations.lockAcquire == false" <<<"$out" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-not-due.lock" ]'

python3 - "$ROOT" <<'PY'
import importlib.util
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location("agent_cron_redaction_test", root / "scripts" / "agent_cron.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module._canonical_redaction is not None
assert Path(module._canonical_redaction.__file__).resolve() == (
    root / "bridge" / "utils" / "redaction.py"
).resolve()

telegram = "123456789:" + "A" * 24
aws = "AKIA" + "B" * 16
github = "ghp_" + "C" * 24
github_pat = "github_pat_" + "D" * 24
openai = "sk-" + "E" * 24
private_key = (
    "-----BEGIN PRIVATE KEY-----\n" + "F" * 80 + "\n-----END PRIVATE KEY-----"
)
truncated_key = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "G" * 80
short_bearer = "Bearer " + "H" * 10
assignment = "passwd=" + "I" * 8
slack = "xoxb-" + "J" * 20
generic = "K" * 30
cases = (
    telegram,
    aws,
    github,
    github_pat,
    openai,
    private_key,
    truncated_key,
    short_bearer,
    assignment,
    slack,
    generic,
)
for raw in cases:
    redacted = module.redact_for_owner("prefix " + raw + " suffix")
    assert redacted is not None
    assert raw not in redacted
    assert module._canonical_redaction.REDACTION_MARKER in redacted
    assert module.redact_for_owner(redacted) == redacted

boundary = ("safe " * 170) + private_key + "tail"
bounded = module.redact_for_owner(boundary, 900)
assert bounded is not None and private_key not in bounded
assert module._canonical_redaction.REDACTION_MARKER in bounded
json.loads(json.dumps({"text": module.redact_for_owner("safe\0text")}))

# Filesystem paths must survive the long-run catch-all. Masking them cost the
# 2026-07-30 fleet-doctor sweep its only diagnostic field: the notification read
# `DRIFT gongmyoung doctor_exit=1 [REDACTED_CREDENTIAL]`, where the marker had
# eaten `runtime=/home/gongmyoung/ccc-node`.
kept = (
    "DRIFT gongmyoung doctor_exit=1 runtime=/home/gongmyoung/ccc-node",
    "DRIFT gongyung doctor_exit=1 runtime=/data/data/com.termux/files/home/ccc-node",
    "DRIFT yukson doctor_exit=1 runtime=/root/ccc-node",
    "drifted: /root/.claude/hooks/lifecycle-feed.sh",
    "OK seoseo (/opt/ccc-node)",
)
for raw in kept:
    assert module.redact_for_owner(raw) == raw, raw

# ...without turning the path exemption into a smuggling channel. The invariant:
# no run of _OWNER_LONG_RUN_MIN+ token characters survives, and every preserved
# path segment is shorter than that threshold on its own.
threshold = module._OWNER_LONG_RUN_MIN
masked = (
    "K" * 30,                                   # plain long run
    "/AbCdEfGhIjKlMnOpQrStUvWxYz012/x",         # slashes, but no allowlisted root
    "https://api.example.com/v1/" + "T" * 30,   # URL path is not a filesystem root
    "K" * 30 + "/root/ccc-node",                # blob adjacent to a real path
    "/tmp/ghp_" + "A" * 22,                     # credential inside a path segment
    "secret=/root/" + "C" * 20,                 # assignment wrapping a path
    "/root/" + "D" * 20 + "==",                 # base64 padding breaks the segment
    "/root/" + "E" * 30,                        # over-long leaf segment
    "/root/" + "F" * 30 + "/x",                 # over-long segment mid-path
    "backup=/root/.claude/backups/ccc-doctor-files-20260730-145600.tar.gz",
)
for raw in masked:
    out = module.redact_for_owner(raw)
    assert module._canonical_redaction.REDACTION_MARKER in out, raw
    residue = out.replace(module._canonical_redaction.REDACTION_MARKER, " ")
    longest = max((len(s) for s in re.findall(r"[A-Za-z0-9_.-]+", residue)), default=0)
    assert longest < threshold, (raw, out, longest)
    assert module.redact_for_owner(out) == out, raw
PY
rc=$?
ok "owner redaction uses exact canonical source and preserves broader hardening" '[ "$rc" = 0 ]'

# --- #829: fleet diagnostics are domain alerts, not generic cron failures ---
out="$(python3 - "$ROOT" "$TMP/fleet-alert-spool" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
spool = Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location(
    "agent_cron_fleet_alert_test", root / "scripts" / "agent_cron.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

task_id = "adapter-fleet-watch"
headless = {
    "exitCode": 1,
    "stdout": (
        "OK healthy-node (/safe/path)\n"
        "DOWN private-node free-form detail\n"
        "UNREACHABLE secret-node credentials=should-not-title\n"
        "DOWN duplicate-node /private/path\n"
        "prefix DRIFT ignored\n"
        " DOWN ignored-leading-space\n"
        "DOWNSTREAM ignored-prefix\n"
    ),
    "stderr": (
        "DRIFT hidden-node doctor_exit=1 runtime=/home/hidden/ccc-node\n"
        "BOOTPATH hidden-node unit=/secret/unit runtime=/secret/runtime\n"
        "DRIFT second-hidden-node api_key=SECRET_LOOKING_VALUE\n"
    ),
}
expected_title = (
    "agent-cron fleet alert for task adapter-fleet-watch: "
    "DOWN=2 UNREACHABLE=1 DRIFT=2 BOOTPATH=1"
)

failed = module.build_owner_text(
    task_id, "run-failed", "2026-01-01T00:00:00Z", "failed", headless
)
assert failed is not None
failed_lines = failed.splitlines()
assert failed_lines[0] == expected_title, failed_lines[0]
assert "private-node" not in failed_lines[0]
assert "/private/path" not in failed_lines[0]
assert "SECRET_LOOKING_VALUE" not in failed_lines[0]
assert "ignored" not in failed_lines[0]
assert "SECRET_LOOKING_VALUE" not in failed

# All non-success states use the same alert classification, including timeout.
timed_out = module.build_owner_text(
    "fleet-doctor-sweep", "run-timeout", "2026-01-01T00:00:00Z", "timeout",
    {"exitCode": 124, "stdout": "", "stderr": "UNREACHABLE private-node\n"},
)
assert timed_out is not None
assert timed_out.splitlines()[0] == (
    "agent-cron fleet alert for task fleet-doctor-sweep: UNREACHABLE=1"
)

# A failure without a recognized line-start token retains the existing fallback.
generic = module.build_owner_text(
    "ordinary-task", "run-generic", "2026-01-01T00:00:00Z", "failed",
    {"exitCode": 1, "stdout": "OK node\nprefix DOWN node", "stderr": "ordinary error"},
)
assert generic is not None
assert generic.splitlines()[0] == (
    "agent-cron task ordinary-task finished with status=failed"
)
leading_space = module.build_owner_text(
    "ordinary-task", "run-leading", "2026-01-01T00:00:00Z", "failed",
    {"exitCode": 1, "stdout": " DOWN hidden-node\nDRIFTED hidden-node", "stderr": ""},
)
assert leading_space is not None
assert leading_space.splitlines()[0] == (
    "agent-cron task ordinary-task finished with status=failed"
)

# Success text is unchanged even if successful output happens to contain a token.
success = module.build_owner_text(
    task_id, "run-success", "2026-01-01T00:00:00Z", "success",
    {"exitCode": 0, "stdout": "DOWN diagnostic-only", "stderr": ""},
)
assert success is not None
assert success.splitlines()[0] == (
    "agent-cron task adapter-fleet-watch finished with status=success"
)

# Invalid ids cannot be promoted into the structured domain-alert title.
invalid = module.build_owner_text(
    "bad id\nINJECTED", "run-invalid", "2026-01-01T00:00:00Z", "failed",
    {"exitCode": 1, "stdout": "DOWN hidden-node", "stderr": ""},
)
assert invalid is not None
assert "fleet alert" not in invalid.splitlines()[0]

os.environ["CCC_PUSH_SPOOL"] = str(spool)
owner = module.write_owner_spool(
    {"notify": "telegram-owner-on-failure"}, task_id, "run-owner",
    "2026-01-01T00:00:00Z", "failed", headless, None,
)
assert owner["delivery"] == "spooled"

chat_id = "-1001234567890"
os.environ["CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS"] = chat_id
chat = module.write_owner_spool(
    {"notify": "telegram-chat-on-failure", "notifyChatId": chat_id},
    task_id, "run-chat", "2026-01-01T00:00:00Z", "failed", headless, None,
)
assert chat["delivery"] == "spooled"

payloads = [
    json.loads(path.read_text(encoding="utf-8"))
    for path in spool.glob("*.json")
]
assert len(payloads) == 2
assert {payload["recipient"] for payload in payloads} == {"owner", "chat"}
for payload in payloads:
    assert payload["text"].splitlines()[0] == expected_title
    assert "SECRET_LOOKING_VALUE" not in payload["text"]
    assert payload["dedup"] == f"agent-cron:{task_id}:{payload['runId']}:failed"
    assert payload["version"] == 1

print(json.dumps({"title": expected_title, "payloads": len(payloads)}, sort_keys=True))
PY
)"; rc=$?
ok "non-success fleet signals produce bounded allowlist-only owner/chat alert titles" \
  '[ "$rc" = 0 ] && jq -e ".payloads == 2 and (.title | contains(\"DOWN=2 UNREACHABLE=1 DRIFT=2 BOOTPATH=1\"))" <<<"$out" >/dev/null'

ISOLATED="$TMP/redaction-unavailable"
mkdir -p "$ISOLATED/scripts" "$ISOLATED/bridge/utils"
cp "$ROOT/scripts"/agent_cron*.py "$ISOLATED/scripts/"
# agent_cron imports its sibling ccc_secure_fs adapter (#1484), which loads
# bridge/utils/secure_fs.py; ship both so only redaction.py is absent here.
cp "$ROOT/scripts/ccc_secure_fs.py" "$ISOLATED/scripts/"
cp "$ROOT/bridge/utils/secure_fs.py" "$ISOLATED/bridge/utils/"
out="$(python3 - "$ISOLATED" "$TMP/redaction-unavailable-spool" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
spool = Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location("isolated_agent_cron", root / "scripts" / "agent_cron.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module._canonical_redaction is None
os.environ["CCC_PUSH_SPOOL"] = str(spool)
secret = "ghp_" + "Z" * 24
result = module.write_owner_spool(
    {"notify": "telegram-owner-on-failure"},
    "task",
    "run",
    "2026-01-01T00:00:00Z",
    "failed",
    {"exitCode": 1, "stdout": "DOWN hidden-node " + secret, "stderr": ""},
    None,
)
assert result["delivery"] == "blocked-redaction-unavailable"
assert result["reason"] == "canonical-redaction-unavailable"
assert not spool.exists()
print(json.dumps(result, sort_keys=True))
PY
)"; rc=$?
ok "missing canonical redactor blocks only notification without stale fallback" \
  '[ "$rc" = 0 ] && jq -e ".delivery == \"blocked-redaction-unavailable\" and .redacted == false" <<<"$out" >/dev/null && ! grep -q "ZZZZZZZZ" <<<"$out"'

mkdir -p "$ISOLATED/bridge/utils"
cat > "$ISOLATED/bridge/utils/redaction.py" <<'PY'
raise RuntimeError("broken canonical redaction fixture")
PY
out="$(python3 - "$ISOLATED" "$TMP/broken-redaction-spool" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
spool = Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location("broken_redaction_agent_cron", root / "scripts" / "agent_cron.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module._canonical_redaction is None
os.environ["CCC_PUSH_SPOOL"] = str(spool)
secret = "sk-" + "Y" * 24
result = module.write_owner_spool(
    {"notify": "telegram-owner"},
    "task",
    "run",
    "2026-01-01T00:00:00Z",
    "success",
    {"exitCode": 0, "stdout": secret, "stderr": ""},
    None,
)
assert result["delivery"] == "blocked-redaction-unavailable"
assert not spool.exists()
print(json.dumps(result, sort_keys=True))
PY
)"; rc=$?
ok "broken canonical module also blocks notification without body leakage" \
  '[ "$rc" = 0 ] && jq -e ".delivery == \"blocked-redaction-unavailable\"" <<<"$out" >/dev/null && ! grep -q "YYYYYYYY" <<<"$out"'

cp "$ROOT/scripts/agent-cron.sh" "$ISOLATED/scripts/"
mkdir -p "$ISOLATED/schemas"
cp "$ROOT/schemas/agent-cron-task-store.schema.json" "$ISOLATED/schemas/"
cat > "$ISOLATED/tasks.json" <<'JSON'
{"version":1,"tasks":[{"id":"no-redactor","schedule":"* * * * *","prompt":"Safe run","enabled":true,"notify":"telegram-owner","lastRunAt":"2026-01-01T00:00:00Z"}]}
JSON
cat > "$ISOLATED/fake-headless.sh" <<'SH'
#!/usr/bin/env bash
printf 'ghp_%024d\n' 0
SH
chmod +x "$ISOLATED/fake-headless.sh"
out="$(CCC_AGENT_CRON_STORE="$ISOLATED/tasks.json" \
  CCC_HEADLESS_CMD="$ISOLATED/fake-headless.sh" \
  CCC_PUSH_SPOOL="$TMP/redaction-unavailable-spool" \
  bash "$ISOLATED/scripts/agent-cron.sh" run no-redactor \
  --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "redactor failure preserves task execution and history while suppressing spool" \
  '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"no-redactor\" and .lastStatus == \"success\" and .runHistory[0].notifyState == \"blocked-redaction-unavailable\")" "$ISOLATED/tasks.json" >/dev/null && [ ! -e "$TMP/redaction-unavailable-spool" ] && ! grep -q "00000000000000000000" <<<"$out"'

SPOOL="$TMP/agent-spool"
out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$SPOOL" bash "$CMD" run exec-notify --json --at 2026-01-01T00:03:00Z)"; rc=$?
ok "run writes owner-only redacted spool for successful notify task" '[ "$rc" = 0 ] && jq -e ".ok == true and .notification.policy == \"telegram-owner\" and .notification.delivery == \"spooled\" and .notification.redacted == true and .mutations.pushSpoolWrite == true" <<<"$out" >/dev/null && [ "$(find "$SPOOL" -maxdepth 1 -type f -name "*.json" | wc -l)" = 1 ]'
ok "spool payload is owner-only, canonically redacted, and bridge-compatible" 'f="$(find "$SPOOL" -maxdepth 1 -type f -name "*.json" | head -1)"; jq -e ".event == \"AgentCronRun\" and .recipient == \"owner\" and .taskId == \"exec-notify\" and .status == \"success\" and (.text | contains(\"abcdefghijklmnopqrstuvwxyz1234567890\") | not) and (.text | contains(\"[REDACTED_CREDENTIAL]\") )" "$f" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$SPOOL" bash "$CMD" run exec-notify-fail --json --at 2026-01-01T00:03:00Z 2>&1)"; rc=$?
ok "run writes owner-only spool for failed notify task" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"failed\" and .notification.delivery == \"spooled\" and .mutations.pushSpoolWrite == true" <<<"$out" >/dev/null && find "$SPOOL" -maxdepth 1 -type f -name "*.json" -print0 | xargs -0 jq -s -e "any(.[]; .taskId == \"exec-notify-fail\" and .status == \"failed\" and .recipient == \"owner\")" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-notify-fail.lock" ]'

BAD_SPOOL="$TMP/not-a-dir-spool"
printf 'not a directory' > "$BAD_SPOOL"
out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$BAD_SPOOL" bash "$CMD" run exec-notify --json --at 2026-01-01T00:04:00Z)"; rc=$?
ok "spool write failure does not prevent run success or lock release" '[ "$rc" = 0 ] && jq -e ".ok == true and .notification.delivery == \"spool-error\" and .mutations.pushSpoolWrite == false" <<<"$out" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-notify.lock" ]'


out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-history --json --at 2026-01-01T00:05:00Z)"; rc=$?
ok "run appends durable runHistory entry on success" '[ "$rc" = 0 ] && jq -e ".mutations.historyAppend == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"exec-history\" and (.runHistory|length)==1 and .runHistory[0].status == \"success\" and .runHistory[0].scheduledAt == \"2026-01-01T00:05:00Z\" and .runHistory[0].exitCode == 0 and .runHistory[0].attempt == 1 and .runHistory[0].notifyState == \"none\")" "$EXEC_STORE" >/dev/null'
ok "runHistory keeps lastRun mirror in sync" 'jq -e ".tasks[] | select(.id == \"exec-history\" and .lastRunAt == .runHistory[-1].scheduledAt and .lastStatus == .runHistory[-1].status and .lastRunId == .runHistory[-1].runId)" "$EXEC_STORE" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-fail --json --at 2026-01-01T00:05:00Z 2>&1)"; rc=$?
ok "run appends durable runHistory entry on failure" '[ "$rc" = 1 ] && jq -e ".mutations.historyAppend == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"exec-fail\" and (.runHistory|length)>=1 and .runHistory[-1].status == \"failed\" and .runHistory[-1].exitCode == 7)" "$EXEC_STORE" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-history-prune --json --at 2026-01-01T00:05:00Z)"; rc=$?
ok "runHistory prunes oldest entries using maxRunHistory" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"exec-history-prune\" and (.runHistory|length)==2 and .runHistory[0].runId == \"old-2\" and .runHistory[1].status == \"success\")" "$EXEC_STORE" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-retry --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "run failure schedules retryEligibleAt with bounded policy" '[ "$rc" = 1 ] && jq -e ".status == \"failed\" and .retry.retryEligibleAt == \"2026-01-01T00:02:00Z\" and .retry.attempt == 1" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"exec-retry\" and .retryState.retryEligibleAt == \"2026-01-01T00:02:00Z\" and .retryState.attempt == 1)" "$EXEC_STORE" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-retry-success --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "successful retry clears retryState" '[ "$rc" = 0 ] && jq -e ".status == \"success\" and .retry.cleared == true" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"exec-retry-success\" and ((has(\"retryState\") | not) or .retryState == null))" "$EXEC_STORE" >/dev/null'

STATUS_STORE="$TMP/status-store/tasks.json"
mkdir -p "$(dirname "$STATUS_STORE")"
cat > "$STATUS_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"status-healthy","schedule":"0 0 * * *","prompt":"ok","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lastStatus":"success"},{"id":"status-failed","schedule":"0 0 * * *","prompt":"bad","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lastStatus":"failed"},{"id":"status-retry-exhausted","schedule":"0 0 * * *","prompt":"retry","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","retryPolicy":{"maxAttempts":1},"retryState":{"scheduledAt":"2026-01-01T00:00:00Z","attempt":1,"retryEligibleAt":"2026-01-01T00:01:00Z","lastStatus":"failed","lastRunId":"r1"}}]}
JSON
out="$(CCC_AGENT_CRON_STORE="$STATUS_STORE" CCC_NODE="test-node" bash "$CMD" status --json --at 2026-01-01T00:05:00Z)"; rc=$?
ok "status --json emits read-only operator rollup" '[ "$rc" = 0 ] && jq -e ".mode == \"status-read-only\" and .mutations.lockAcquire == false and .mutations.taskStoreWrite == false and .mutations.pushSpoolWrite == false" <<<"$out" >/dev/null'
ok "status reports failed and retry-exhausted tasks" 'jq -e ".tasks[] | select(.id == \"status-failed\" and .health == \"failed\" and .node == \"test-node\")" <<<"$out" >/dev/null && jq -e ".tasks[] | select(.id == \"status-retry-exhausted\" and .health == \"retry-exhausted\" and .retryExhausted == true)" <<<"$out" >/dev/null'

# --- #665: group/channel (telegram-chat) notify delivery + allowlist ---
CHAT_CRUD="$TMP/chat-crud/tasks.json"
out="$(CCC_AGENT_CRON_STORE="$CHAT_CRUD" bash "$CMD" add chat-report --schedule "0 9 * * *" --prompt "Weekly report" --notify telegram-chat --notify-chat-id -1001234567890 --json)"; rc=$?
ok "add accepts telegram-chat with a chat id" '[ "$rc" = 0 ] && jq -e ".tasks[] | select(.id == \"chat-report\" and .notify == \"telegram-chat\" and .notifyChatId == \"-1001234567890\")" "$CHAT_CRUD" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$TMP/chat-crud2/tasks.json" bash "$CMD" add chat-noid --schedule "@daily" --prompt "x" --notify telegram-chat --json 2>&1)"; rc=$?
ok "add rejects telegram-chat without a chat id" '[ "$rc" != 0 ]'

CHAT_STORE="$TMP/chat-store/tasks.json"; mkdir -p "$(dirname "$CHAT_STORE")"
cat > "$CHAT_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"chat-run","schedule":"* * * * *","prompt":"post report","enabled":true,"notify":"telegram-chat","notifyChatId":"-1001234567890","lastRunAt":"2026-01-01T00:00:00Z"}]}
JSON
CHAT_SPOOL="$TMP/chat-spool"
out="$(CCC_AGENT_CRON_STORE="$CHAT_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$CHAT_SPOOL" CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS="-1001234567890" bash "$CMD" run chat-run --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "allowlisted telegram-chat spools with recipient=chat and chatId" '[ "$rc" = 0 ] && jq -e ".notification.delivery == \"spooled\"" <<<"$out" >/dev/null && f="$(find "$CHAT_SPOOL" -maxdepth 1 -type f -name "*.json" | head -1)" && jq -e ".recipient == \"chat\" and .chatId == \"-1001234567890\" and .event == \"AgentCronRun\"" "$f" >/dev/null'

CHAT_SPOOL2="$TMP/chat-spool-blocked"
out="$(CCC_AGENT_CRON_STORE="$CHAT_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$CHAT_SPOOL2" bash "$CMD" run chat-run --json --at 2026-01-01T00:02:00Z)"; rc=$?
ok "non-allowlisted telegram-chat is blocked, nothing spooled" '[ "$rc" = 0 ] && jq -e ".notification.delivery == \"blocked-not-allowlisted\"" <<<"$out" >/dev/null && [ -z "$(ls -A "$CHAT_SPOOL2" 2>/dev/null)" ]'

# ---- unknown task id: reason, not a traceback -----------------------------
# The failure shapes carry only ok/mode/taskId/error; emit_run read 'at' from
# them, so a typo'd task id produced KeyError: 'at' instead of the reason, and
# text mode never printed the reason even when it survived.
out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" run no-such-task --at 2026-01-01T00:02:00Z 2>&1)"; rc=$?
ok "unknown task id exits 1" '[ "$rc" = 1 ]'
ok "unknown task id reports the reason" 'grep -q "task id not found" <<<"$out"'
ok "unknown task id does not traceback" '! grep -qE "Traceback|KeyError" <<<"$out"'
ok "unknown task id still names the task" 'grep -q "no-such-task" <<<"$out"'

out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" run no-such-task --dry-run --at 2026-01-01T00:02:00Z 2>&1)"; rc=$?
ok "unknown task id (dry-run) exits 1" '[ "$rc" = 1 ]'
ok "unknown task id (dry-run) does not traceback" '! grep -qE "Traceback|KeyError" <<<"$out"'

out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" run no-such-task --json --at 2026-01-01T00:02:00Z 2>&1)"; rc=$?
ok "unknown task id --json stays machine-readable" '[ "$rc" = 1 ] && jq -e ".ok == false and .error == \"task id not found\"" <<<"$out" >/dev/null'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
