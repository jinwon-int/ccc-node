#!/usr/bin/env bash
# Delegated-approval Phase 1 shadow recorder (a2a-nexus#2317, ccc-node#2137) — hermetic.
# Wires codex/skills/gh-pr-flow/scripts/delegated_approval_shadow_test.py into the hook-test
# and delegated_approval_reconcile_test.py into the hook-test suite and checks every entrypoint
# parses --help. No gh, no ssh, no network.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
D="$ROOT/codex/skills/gh-pr-flow/scripts"
pass=0; fail=0
TMP="$(mktemp -d)" || exit 1
trap 'rm -rf "$TMP"' EXIT
if python3 "$D/delegated_approval_shadow_test.py" >"$TMP/unit.out" 2>&1; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: shadow recorder unit suite"; tail -30 "$TMP/unit.out"; fi
if python3 "$D/delegated_approval_reconcile_test.py" >"$TMP/unit2.out" 2>&1; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: collect/reconcile unit suite"; tail -30 "$TMP/unit2.out"; fi
for sub in "collect" "reconcile"; do
  if python3 "$D/delegated_approval_reconcile.py" "$sub" --help >/dev/null 2>&1; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: reconcile.py $sub --help"; fi
done
for sub in "record" "ask" "decide" "summary"; do
  if python3 "$D/delegated_approval_shadow.py" "$sub" --help >/dev/null 2>&1; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $sub --help"; fi
done
# The recorder must never write to stdout in record mode (the caller's stdout is the approval JSON).
printf '{"files":[{"path":"docs/a.md"}],"additions":1,"deletions":0,"title":"docs (#1)","author":{"login":"jinon86"},"baseRefName":"main"}' >"$TMP/pr.json"
out="$(python3 "$D/delegated_approval_shadow.py" record --repo jinwon-int/ccc-node --pr 1 --head "$(printf 'a%.0s' $(seq 40))" --approver seoseo-ai --pr-json-file "$TMP/pr.json" --state-dir "$TMP/state" 2>/dev/null)"
if [ -z "$out" ] && [ -s "$TMP/state/delegated-approval/shadow.jsonl" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: record wrote to stdout or no ledger"; fi
# Same rule for ask/decide (spec §10.2 5-d): quiet stdout, one ledger row each, never a failing exit.
H="$(printf 'b%.0s' $(seq 40))"
out="$(python3 "$D/delegated_approval_shadow.py" ask --repo jinwon-int/ccc-node --pr 2 --head "$H" --pr-json-file "$TMP/pr.json" --state-dir "$TMP/state" 2>/dev/null)"; rc_ask=$?
out="$out$(python3 "$D/delegated_approval_shadow.py" decide --repo jinwon-int/ccc-node --pr 2 --decision decline --source reply --state-dir "$TMP/state" 2>/dev/null)"; rc_dec=$?
if [ -z "$out" ] && [ "$rc_ask$rc_dec" = "00" ] && [ "$(grep -c '"kind": "ask"\|"kind": "decision"' "$TMP/state/delegated-approval/shadow.jsonl")" = 2 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: ask/decide wrote to stdout, failed, or left no rows"; fi
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
