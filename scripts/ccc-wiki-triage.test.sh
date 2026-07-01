#!/usr/bin/env bash
# No-network smoke tests for ccc-wiki-triage.sh. It writes decisions only under CCC_STATE_DIR.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"; mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-wiki-triage-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

state="$TMP/state"; mkdir -p "$state"
cat > "$state/wiki-candidates.md" <<'MD'
## CAND-001 Memory doc update
Useful durable fact.
api_key=should_not_print

## CAND-002 Hold for review
No secret here.
MD

out="$(CCC_STATE_DIR="$state" bash "$ROOT/scripts/ccc-wiki-triage.sh" list)"; rc=$?
ok "triage list emits candidates as JSON" '[ "$rc" = 0 ] && jq -e ".ok == true and .count == 2 and .candidates[0].redaction_applied == true" <<<"$out" >/dev/null'
ok "triage list does not expose sensitive line" '! grep -q "should_not_print" <<<"$out" && grep -q "redaction_applied" <<<"$out"'

out="$(CCC_STATE_DIR="$state" bash "$ROOT/scripts/ccc-wiki-triage.sh" show CAND-001)"; rc=$?
ok "triage show redacts candidate body" '[ "$rc" = 0 ] && jq -e ".candidate.body | contains(\"[REDACTED_SENSITIVE_LINE]\")" <<<"$out" >/dev/null && ! grep -q "should_not_print" <<<"$out"'

out="$(CCC_STATE_DIR="$state" bash "$ROOT/scripts/ccc-wiki-triage.sh" mark-held CAND-002)"; rc=$?
ok "triage mark-held writes only local decision file" '[ "$rc" = 0 ] && jq -e ".ok == true and .wiki_write_performed == false" <<<"$out" >/dev/null && jq -e ".\"CAND-002\".decision == \"held\"" "$state/wiki-candidate-decisions.json" >/dev/null'

out="$(CCC_STATE_DIR="$state" bash "$ROOT/scripts/ccc-wiki-triage.sh" show missing 2>&1)"; rc=$?
ok "triage missing candidate fails closed" '[ "$rc" = 1 ] && jq -e ".ok == false and .error == \"candidate not found\"" <<<"$out" >/dev/null'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
