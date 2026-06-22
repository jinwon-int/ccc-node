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


EXEC_STORE="$TMP/exec-store/tasks.json"
mkdir -p "$(dirname "$EXEC_STORE")"
cat > "$EXEC_STORE" <<'JSON'
{"version":1,"tasks":[{"id":"exec-success","schedule":"* * * * *","prompt":"Run safely","enabled":true,"notify":"none","allowedTools":["Read","Grep"],"permissionMode":"dontAsk","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-fail","schedule":"* * * * *","prompt":"Fail safely","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-disabled","schedule":"* * * * *","prompt":"Disabled","enabled":false,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-not-due","schedule":"0 0 * * *","prompt":"Not due","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-notify","schedule":"* * * * *","prompt":"Notify safely","enabled":true,"notify":"telegram-owner","redactProfile":"owner","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-notify-fail","schedule":"* * * * *","prompt":"Notify Fail secret","enabled":true,"notify":"telegram-owner","redactProfile":"owner","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60},{"id":"exec-history","schedule":"* * * * *","prompt":"History safely","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60,"maxRunHistory":3},{"id":"exec-history-prune","schedule":"* * * * *","prompt":"History prune","enabled":true,"notify":"none","lastRunAt":"2026-01-01T00:00:00Z","lockTimeoutSec":60,"maxRunHistory":2,"runHistory":[{"runId":"old-1","scheduledAt":"2026-01-01T00:00:00Z","startedAt":"2026-01-01T00:00:00Z","finishedAt":"2026-01-01T00:00:00Z","status":"success","exitCode":0,"attempt":1,"notifyState":"none"},{"runId":"old-2","scheduledAt":"2026-01-01T00:01:00Z","startedAt":"2026-01-01T00:01:00Z","finishedAt":"2026-01-01T00:01:00Z","status":"success","exitCode":0,"attempt":1,"notifyState":"none"}]}]}
JSON
FAKE_HEADLESS="$TMP/fake-headless-exec.sh"
cat > "$FAKE_HEADLESS" <<'SH'
#!/usr/bin/env bash
set -u
printf 'prompt=%s
allowed=%s
perm=%s
' "$1" "${CCC_ALLOWED_TOOLS:-}" "${CCC_PERMISSION_MODE:-}" >> "$FAKE_HEADLESS_LOG"
case "$1" in
  *Fail*) echo "fake failure" >&2; exit 7 ;;
  *) echo "fake result for $1 token=abcdefghijklmnopqrstuvwxyz1234567890" ;;
esac
SH
chmod +x "$FAKE_HEADLESS"
export FAKE_HEADLESS_LOG="$TMP/fake-headless.log"

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-success --json --at 2026-01-01T00:01:00Z)"; rc=$?
ok "run executes due task with fake headless" '[ "$rc" = 0 ] && jq -e ".ok == true and .status == \"success\" and .mutations.lockAcquire == true and .mutations.taskStoreWrite == true and .mutations.headlessExecute == true" <<<"$out" >/dev/null'
ok "run passes prompt and policy to headless" 'grep -q "prompt=Run safely" "$FAKE_HEADLESS_LOG" && grep -q "allowed=Read,Grep" "$FAKE_HEADLESS_LOG" && grep -q "perm=dontAsk" "$FAKE_HEADLESS_LOG"'
ok "run records last successful state and releases lock" 'jq -e ".tasks[] | select(.id == \"exec-success\" and .lastRunAt == \"2026-01-01T00:01:00Z\" and .lastStatus == \"success\" and (.lastRunId|type == \"string\"))" "$EXEC_STORE" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-success.lock" ]'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" bash "$CMD" run exec-fail --json --at 2026-01-01T00:01:00Z 2>&1)"; rc=$?
ok "run propagates headless failure" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"failed\" and .headless.exitCode == 7" <<<"$out" >/dev/null'
ok "run failure records state and releases lock" 'jq -e ".tasks[] | select(.id == \"exec-fail\" and .lastRunAt == \"2026-01-01T00:01:00Z\" and .lastStatus == \"failed\")" "$EXEC_STORE" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-fail.lock" ]'

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

SPOOL="$TMP/agent-spool"
out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$SPOOL" bash "$CMD" run exec-notify --json --at 2026-01-01T00:03:00Z)"; rc=$?
ok "run writes owner-only redacted spool for successful notify task" '[ "$rc" = 0 ] && jq -e ".ok == true and .notification.policy == \"telegram-owner\" and .notification.delivery == \"spooled\" and .notification.redacted == true and .mutations.pushSpoolWrite == true" <<<"$out" >/dev/null && [ "$(find "$SPOOL" -maxdepth 1 -type f -name "*.json" | wc -l)" = 1 ]'
ok "spool payload is owner-only, redacted, and bridge-compatible" 'f="$(find "$SPOOL" -maxdepth 1 -type f -name "*.json" | head -1)"; jq -e ".event == \"AgentCronRun\" and .recipient == \"owner\" and .taskId == \"exec-notify\" and .status == \"success\" and (.text | contains(\"abcdefghijklmnopqrstuvwxyz1234567890\") | not) and (.text | contains(\"[REDACTED]\") )" "$f" >/dev/null'

out="$(CCC_AGENT_CRON_STORE="$EXEC_STORE" CCC_HEADLESS_CMD="$FAKE_HEADLESS" CCC_PUSH_SPOOL="$SPOOL" bash "$CMD" run exec-notify-fail --json --at 2026-01-01T00:03:00Z 2>&1)"; rc=$?
ok "run writes owner-only spool for failed notify task" '[ "$rc" = 1 ] && jq -e ".ok == false and .status == \"failed\" and .notification.delivery == \"spooled\" and .mutations.pushSpoolWrite == true" <<<"$out" >/dev/null && find "$SPOOL" -maxdepth 1 -type f -name "*.json" -print0 | xargs -0 jq -e "select(.taskId == \"exec-notify-fail\" and .status == \"failed\" and .recipient == \"owner\")" >/dev/null && [ ! -e "$TMP/exec-store/locks/exec-notify-fail.lock" ]'

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
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
