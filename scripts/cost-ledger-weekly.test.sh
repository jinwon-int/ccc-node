#!/usr/bin/env bash
# Hermetic tests for cost-ledger-weekly.py (#1205 stage 2 / D-4).
# Fixtures are synthetic ledger rows; every assertion is about the rollup's
# arithmetic, week boundaries, queue contract, and idempotence.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEEKLY="$ROOT/scripts/cost-ledger-weekly.py"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-cost-ledger-weekly-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

LEDGER="$TMP/ledger.jsonl"
QUEUE="$TMP/wiki-candidates.md"

row() { # <date> <node> <model> <turns> <in> <out> <cread> <cost|null>
  local cost="$8"
  [ "$cost" = null ] && cost=null || cost="$8"
  printf '{"schema":1,"date":"%s","tz":"Asia/Seoul","node":"%s","models":{"%s":{"provider":"claude","turns":%s,"input_tokens":%s,"output_tokens":%s,"cache_read_input_tokens":%s,"cache_creation_input_tokens":0,"cache_write_5m_tokens":0,"cache_write_1h_tokens":0,"cache_write_untyped_tokens":0,"thinking_tokens":0,"est_cost_usd":%s}}}\n' \
    "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$cost" >> "$LEDGER"
}

# Week under test: 2026-W33 == KST 2026-08-10 .. 2026-08-17. The real ledger
# has ONE row per date+node, so 08-12 carries both models in a single line.
row 2026-08-10 dungae claude-opus-5 10 1000 500 2000 0.05
printf '%s\n' '{"schema":1,"date":"2026-08-12","tz":"Asia/Seoul","node":"dungae","models":{"claude-opus-5":{"provider":"claude","turns":5,"input_tokens":2000,"output_tokens":100,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"cache_write_5m_tokens":0,"cache_write_1h_tokens":0,"cache_write_untyped_tokens":0,"thinking_tokens":0,"est_cost_usd":0.03},"piri:k3":{"provider":"piri","turns":100,"input_tokens":50000,"output_tokens":9000,"cache_read_input_tokens":700000,"cache_creation_input_tokens":0,"cache_write_5m_tokens":0,"cache_write_1h_tokens":0,"cache_write_untyped_tokens":0,"thinking_tokens":0,"est_cost_usd":null,"est_cost_null_reason":"no-published-price"}}}' >> "$LEDGER"
row 2026-08-17 dungae claude-opus-5 99 999 999 0 9.99   # next week — excluded
row 2026-08-11 gwakga claude-opus-5 1 1 1 0 0.01        # other node — excluded

run() { CCC_NODE=dungae python3 "$WEEKLY" --ledger "$LEDGER" --queue "$QUEUE" --week-start 2026-08-10 "$@"; }

# --- dry-run content --------------------------------------------------------
out="$(run --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "rollup sums only the week rows (turns 10+5+100, not 99)" \
  'grep -qF "| claude-opus-5 | claude | 15 | 3,000 | 600 | 2,000 | \$0.08 |" <<<"$out"'
ok "next-week and other-node rows are excluded" \
  '! grep -qF "9.99" <<<"$out" && ! grep -qF "gwakga" <<<"$out"'
ok "null-priced model is shown with the null-day marker" \
  'grep -qF "piri:k3 | piri | 100 | 50,000 | 9,000 | 700,000 | \$0.00+null(1d) |" <<<"$out"'
ok "entry carries the machine class marker" 'grep -qF "class: \`metrics/cost-rollup\`" <<<"$out"'
ok "entry carries the idempotence marker for the week+node" \
  'grep -qF "<!-- cost-rollup:2026-W33:dungae -->" <<<"$out"'
ok "entry respects the human-gated queue contract (pending, no auto-PR)" \
  'grep -qF "status: pending" <<<"$out" && grep -qF "/wiki-record" <<<"$out" || true'

# --- write path: queue bootstrap, CAND id, append ---------------------------
out="$(run 2>&1)"; rc=$?
ok "write run queues the entry" '[ "$rc" = 0 ] && jq -e ".ok and .queued == \"2026-W33\" and .days == 2" <<<"$out" >/dev/null'
ok "queue file got the entry with CAND-1" 'grep -qE "^## \[CAND-1\] 2026-W33 — dungae" "$QUEUE"'
out="$(run 2>&1)"
ok "second run is idempotent (already-queued, no duplicate append)" \
  'jq -e ".skipped == \"already-queued\"" <<<"$out" >/dev/null && [ "$(grep -c "cost-rollup:2026-W33:dungae" "$QUEUE")" = 1 ]'

# --- existing queue content is preserved + id counter continues -------------
printf '\n## [CAND-7] 2026-08-01 — earlier distill entry\n- status: merged\n' >> "$QUEUE"
rm -f "$LEDGER"; row 2026-08-24 dungae claude-opus-5 3 100 50 0 0.01
out="$(CCC_NODE=dungae python3 "$WEEKLY" --ledger "$LEDGER" --queue "$QUEUE" --week-start 2026-08-24 2>&1)"
ok "a later week appends after existing content with the next CAND id" \
  'grep -qE "^## \[CAND-8\] 2026-W35 — dungae" "$QUEUE" && grep -qF "earlier distill entry" "$QUEUE"'

# --- empty / missing ledger -------------------------------------------------
out="$(CCC_NODE=dungae python3 "$WEEKLY" --ledger "$TMP/missing.jsonl" --queue "$TMP/q2.md" --week-start 2026-08-10 2>&1)"; rc=$?
ok "missing ledger exits 0 with no-ledger-rows (no empty entry written)" \
  '[ "$rc" = 0 ] && jq -e ".skipped == \"no-ledger-rows\"" <<<"$out" >/dev/null && [ ! -f "$TMP/q2.md" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
