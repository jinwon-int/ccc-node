#!/usr/bin/env bash
# Tests for ccc-memory-eval.py — offline eval harness validation.
# Uses fixtures only; NO live Honcho or provider calls. Runs in CI mode.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EVAL="$HERE/ccc-memory-eval.py"
pass=0; fail=0

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# ── full suite ─────────────────────────────────────────────────────
export CI=true
out="$(python3 "$EVAL" 2>&1)"; rc=$?
ok "full suite exits 0" '[ "$rc" = 0 ]'
ok "full suite has eval_version" 'jq -e ".eval_version" <<<"$out" >/dev/null'
ok "full suite has timestamp" 'jq -e ".timestamp_utc" <<<"$out" >/dev/null'
ok "full suite reports is_ci" 'jq -e ".is_ci == true" <<<"$out" >/dev/null'
ok "full suite has latency block" 'jq -e ".latency" <<<"$out" >/dev/null'
ok "full suite has token-budget block" 'jq -e ".[\"token-budget\"]" <<<"$out" >/dev/null'
ok "full suite has refresh-health block" 'jq -e ".[\"refresh-health\"]" <<<"$out" >/dev/null'
ok "full suite has recall block" 'jq -e ".recall" <<<"$out" >/dev/null'
ok "full suite has summary" 'jq -e ".summary" <<<"$out" >/dev/null'

# ── latency suite ───────────────────────────────────────────────────
out="$(CI=true python3 "$EVAL" --suite latency 2>&1)"; rc=$?
ok "latency suite exits 0" '[ "$rc" = 0 ]'
ok "latency suite has update latency_s" 'jq -e ".latency.update.latency_s >= 0" <<<"$out" >/dev/null'
ok "latency suite has search latency samples" 'jq -e ".latency.search.samples >= 1" <<<"$out" >/dev/null'
ok "latency suite has check latency_s" 'jq -e ".latency.check.latency_s >= 0" <<<"$out" >/dev/null'
ok "latency suite indexed 4 sources" 'jq -e ".latency.update.indexed_sources == 4" <<<"$out" >/dev/null'

# ── recall suite ────────────────────────────────────────────────────
out="$(CI=true python3 "$EVAL" --suite recall 2>&1)"; rc=$?
ok "recall suite exits 0" '[ "$rc" = 0 ]'
ok "recall has mean_precision > 0" 'jq -e ".recall.mean_precision > 0" <<<"$out" >/dev/null'
ok "recall has mean_recall > 0" 'jq -e ".recall.mean_recall > 0" <<<"$out" >/dev/null'
ok "recall has per-query results" 'jq -e ".recall.per_query | length == 5" <<<"$out" >/dev/null'

# ── token-budget suite ──────────────────────────────────────────────
out="$(CI=true python3 "$EVAL" --suite token-budget 2>&1)"; rc=$?
ok "token-budget suite exits 0" '[ "$rc" = 0 ]'
ok "token-budget has per_query entries" 'jq -e ".[\"token-budget\"].per_query | length == 5" <<<"$out" >/dev/null'
ok "token-budget reports total est tokens" 'jq -e ".[\"token-budget\"].total_est_tokens > 0" <<<"$out" >/dev/null'

# ── refresh-health suite ────────────────────────────────────────────
out="$(CI=true python3 "$EVAL" --suite refresh-health 2>&1)"; rc=$?
ok "refresh-health suite exits 0" '[ "$rc" = 0 ]'
ok "refresh-health has coverage" 'jq -e ".[\"refresh-health\"].coverage >= 0" <<<"$out" >/dev/null'
ok "refresh-health has sources" 'jq -e ".[\"refresh-health\"].total_sources >= 1" <<<"$out" >/dev/null'

# ── custom fixtures dir exists (even if empty) ──────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/custom-fixtures"
out="$(CI=true python3 "$EVAL" --fixtures-dir "$TMP/custom-fixtures" --suite recall 2>&1)"; rc=$?
ok "custom fixtures dir does not crash" '[ "$rc" = 0 ]'

# ── multiple suites ─────────────────────────────────────────────────
out="$(CI=true python3 "$EVAL" --suite latency recall 2>&1)"; rc=$?
ok "multi-suite exits 0" '[ "$rc" = 0 ]'
ok "multi-suite has latency" 'jq -e ".latency" <<<"$out" >/dev/null'
ok "multi-suite has recall" 'jq -e ".recall" <<<"$out" >/dev/null'
ok "multi-suite no token-budget" 'jq -e ".[\"token-budget\"] == null" <<<"$out" >/dev/null'

# ── keep-workdir flag ───────────────────────────────────────────────
out="$(CI=true python3 "$EVAL" --suite latency --keep-workdir 2>&1)"; rc=$?
ok "keep-workdir exits 0" '[ "$rc" = 0 ]'
ok "keep-workdir reports workdir" 'jq -e ".workdir" <<<"$out" >/dev/null'
# Verify the workdir was NOT cleaned up (it should still exist)
wd="$(jq -r '.workdir' <<<"$out")"
ok "keep-workdir dir still exists" '[ -d "$wd" ]'
rm -rf "$wd"

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
