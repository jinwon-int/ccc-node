#!/usr/bin/env bash
# Tests for agent-cron store/list first slice — no execution, no push, no scheduler.
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

out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" list --json)"; rc=$?
ok "list --json exits 0" '[ "$rc" = 0 ]'
ok "list --json is valid JSON" 'jq -e ".version == 1 and (.tasks|length)==1" <<<"$out" >/dev/null'

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

before="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
out="$(CCC_AGENT_CRON_STORE="$STORE" bash "$CMD" run daily-wiki-prefetch 2>&1)"; rc=$?
after="$(find "$TMP" -type f -printf '%P %s %T@\n' | sort)"
ok "run is not implemented in first slice" '[ "$rc" = 2 ] && grep -q "not implemented" <<<"$out"'
ok "run made no filesystem changes" '[ "$before" = "$after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
