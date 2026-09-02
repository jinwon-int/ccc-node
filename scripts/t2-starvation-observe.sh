#!/usr/bin/env bash
# ccc-node:t2-starvation-observe — #2024 acceptance: 1-week T2 analysis
# starvation observation (2026-08-31 → 2026-09-07). READ-ONLY: counts
# analysis/intake-intent broker tasks per broker/worker from each broker's
# sqlite (mode=ro) and appends one JSON line per run. Self-expiring; no
# mutations to broker, ledger, or PR state. Runs on seoseo (T1 local, T2
# via SSH).
# v2 (2026-09-02, #2024 follow-up): watch the intent family, not just
# "analyze" — T1's skills-intake lane runs under its own intent name, so
# the analyze-only filter made T1 look dead (gongmyoung live audit).
set -uo pipefail
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
# v2.1 (2026-09-02): repo copy — resolve paths from the environment with
# root-on-seoseo defaults, so the installer-managed entry and a hand-run
# produce byte-identical behavior. Only seoseo satisfies the T1_DB + T2_HOST
# preconditions today; elsewhere the installer is a documented no-op.
T1_DB="${CCC_T2_OBSERVE_T1_DB:-/var/lib/a2a-broker/state.sqlite}"
T2_HOST="${CCC_T2_OBSERVE_T2_HOST:-gwakga}"
END_TS="2026-09-07T15:00:00Z"   # stop after 2026-09-08 00:00 KST
OUT="${CCC_T2_OBSERVE_OUT:-$STATE_DIR/t2-starvation-observation.jsonl}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
[ "$NOW" \> "$END_TS" ] && { echo "observation window closed $NOW"; exit 0; }

QUERY='import sqlite3, json, sys
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
con.row_factory = sqlite3.Row
since = sys.argv[1]
rows = con.execute(
    "select id, status, intent, assigned_worker_id, payload from broker_tasks"
).fetchall()
WATCH = {"analyze", "skills_intake_review", "skills-intake-review", "skills_intake_revise"}
out = {"total": 0, "watch_intents": sorted(WATCH), "window": {}, "by_worker": {}, "window_by_intent": {}}
for r in rows:
    if r["intent"] not in WATCH:
        continue
    out["total"] += 1
    try:
        created = json.loads(r["payload"]).get("createdAt", "")
    except Exception:
        created = ""
    if created >= since:
        key = r["status"] or "unknown"
        out["window"][key] = out["window"].get(key, 0) + 1
        out["window_by_intent"][r["intent"]] = out["window_by_intent"].get(r["intent"], 0) + 1
        w = r["assigned_worker_id"] or "unassigned"
        out["by_worker"][w] = out["by_worker"].get(w, 0) + 1
print(json.dumps(out))'

SINCE=$(python3 -c "import datetime; print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S'))")

T1=$(python3 -c "$QUERY" "$T1_DB" "$SINCE" 2>/dev/null || echo '{"error":"t1-unreachable"}')
T2=$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$T2_HOST" "python3 - '$SINCE' <<'PYEOF'
import sqlite3, json, sys
con = sqlite3.connect('file:/var/lib/a2a-broker/state.sqlite?mode=ro', uri=True)
con.row_factory = sqlite3.Row
since = sys.argv[1]
WATCH = {'analyze', 'skills_intake_review', 'skills-intake-review', 'skills_intake_revise'}
out = {'total': 0, 'watch_intents': sorted(WATCH), 'window': {}, 'by_worker': {}, 'window_by_intent': {}}
for r in con.execute('select id, status, intent, assigned_worker_id, payload from broker_tasks'):
    if r['intent'] not in WATCH:
        continue
    out['total'] += 1
    try:
        created = json.loads(r['payload']).get('createdAt', '')
    except Exception:
        created = ''
    if created >= since:
        k = r['status'] or 'unknown'
        out['window'][k] = out['window'].get(k, 0) + 1
        out['window_by_intent'][r['intent']] = out['window_by_intent'].get(r['intent'], 0) + 1
        w = r['assigned_worker_id'] or 'unassigned'
        out['by_worker'][w] = out['by_worker'].get(w, 0) + 1
print(json.dumps(out))
PYEOF" 2>/dev/null || echo '{"error":"t2-unreachable"}')

python3 -c "
import json, sys
rec = {'ts': '$NOW', 'window_hours': 24, 'observer_version': 2, 't1_primary': json.loads('''$T1'''), 't2_gwakga': json.loads('''$T2''')}
with open('$OUT', 'a') as fh:
    fh.write(json.dumps(rec, ensure_ascii=False) + chr(10))
print(json.dumps(rec, ensure_ascii=False))
"
