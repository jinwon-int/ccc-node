#!/usr/bin/env bash
# Tests for ccc-distill-check.sh — read-only status, no network.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK="$ROOT/scripts/ccc-distill-check.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
mkdir -p "$TMP/state" "$TMP/hermes"
cat > "$TMP/honcho.json" <<'JSON'
{"baseUrl":"http://honcho.example","workspace":"test"}
JSON
export CCC_STATE_DIR="$TMP/state"
export CCC_HONCHO_CFG="$TMP/honcho.json"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "empty state exits 0" '[ "$rc" = 0 ]'
ok "empty state reports live mode" 'jq -e ".mode == \"LIVE\" and .queue.lines == 0 and .checkpoint.snapshots == 0 and .triggers.precompact == 0" <<<"$out" >/dev/null'
ok "empty state reports honcho base without network" 'jq -e ".honcho_base == \"http://honcho.example\"" <<<"$out" >/dev/null'
# Timestamps must stay inside the checker's 14-day window on ANY run date —
# hardcoded dates rot and start failing two weeks after they were written.
D3="$(date -u -d '3 days ago' +%Y-%m-%dT%H:%M:%SZ)"
D2="$(date -u -d '2 days ago' +%Y-%m-%dT%H:%M:%SZ)"
D1="$(date -u -d '1 day ago'  +%Y-%m-%dT%H:%M:%SZ)"
cat > "$TMP/state/distill.log" <<LOG
$D3 start trigger=manual dryrun=0 pid=1
$D3 done trigger=manual pid=2 elapsed_s=1
$D2 start trigger=sessionend dryrun=0 pid=3
$D2 [drain] drained ok=2 failed=1 dropped=1 processed=4
$D1 start trigger=precompact dryrun=0 pid=5
LOG
cat > "$TMP/state/honcho-queue.jsonl" <<'JSONL'
{"session_id":"a"}
{"session_id":"b"}
JSONL
cat > "$TMP/state/honcho-queue.jsonl.dead" <<'JSONL'
{"session_id":"dead"}
JSONL
cat > "$TMP/state/distill-last.json" <<'JSON'
{"session_id":"s1","trigger":"precompact","distilled_at":"2026-06-22T00:00:01Z","honcho":[{"text":"x"}],"wiki_candidates":[{"title":"w"}]}
JSON
mkdir -p "$TMP/state/checkpoints"
printf 'older\n' > "$TMP/state/checkpoints/working-state-20260621_000000.md"
printf 'newer\n' > "$TMP/state/checkpoints/working-state-20260622_000000.md"
touch -t 202606210000 "$TMP/state/checkpoints/working-state-20260621_000000.md"
touch -t 202606220000 "$TMP/state/checkpoints/working-state-20260622_000000.md"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "populated state exits 0" '[ "$rc" = 0 ]'
ok "populated counts queue/dead" 'jq -e ".queue.lines == 2 and .queue.dead == 1" <<<"$out" >/dev/null'
ok "populated counts triggers" 'jq -e ".triggers.manual == 1 and .triggers.sessionend == 1 and .triggers.precompact == 1" <<<"$out" >/dev/null'
ok "populated reports checkpoints" 'jq -e ".checkpoint.snapshots == 2 and (.checkpoint.last | contains(\"working-state-20260622_000000.md\"))" <<<"$out" >/dev/null'
ok "populated counts drain" 'jq -e ".drain.ok == 2 and .drain.failed == 1 and .drain.dropped == 1" <<<"$out" >/dev/null'
ok "populated reports last summary" 'jq -e ".last | contains(\"session=s1\") and contains(\"trigger=precompact\")" <<<"$out" >/dev/null'
touch "$TMP/state/distill.disabled"
out="$(bash "$CHECK" --json 2>&1)"; rc=$?
ok "disabled mode detected" '[ "$rc" = 0 ] && jq -e ".mode == \"OFF\"" <<<"$out" >/dev/null'
rm -f "$TMP/state/distill.disabled"
touch "$TMP/state/distill.dryrun"
out="$(bash "$CHECK" 2>&1)"; rc=$?
ok "text output exits 0" '[ "$rc" = 0 ]'
ok "text output shows dry-run" 'grep -q "mode:" <<<"$out" && grep -q "DRY-RUN" <<<"$out"'
echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
