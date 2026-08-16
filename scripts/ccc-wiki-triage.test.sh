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

# Regression (#869 sweep / #1076): SECRET_LINE only matched `keyword:`/`keyword=`
# shapes, so an unlabelled token or a PEM body printed verbatim through `show`.
# The fake credentials below are assembled at runtime on purpose -- keeping the
# literals out of the file keeps this test off the gitleaks allowlist. Do not
# "simplify" them back into single strings.
gh_tok="ghp""_$(printf 'A%.0s' $(seq 1 36))"
aws_tok="AKIA""$(printf 'B%.0s' $(seq 1 16))"
state2="$TMP/state2"; mkdir -p "$state2"
{
  echo '## CAND-010 Unlabelled token'
  echo 'Pasted from a terminal without any label:'
  echo "$gh_tok"
  echo "$aws_tok"
  echo
  echo '## CAND-011 PEM block'
  echo '-----BEGIN RSA PRIVATE KEY-----'
  echo 'MIIEowIBAAKCAQEAxfakefakefakefakefakefakefakefakefakefakefakeQIDA'
  echo '-----END RSA PRIVATE KEY-----'
  echo
  echo '## CAND-012 Ordinary prose'
  echo 'The broker tunnel is documented in the node runbook.'
} > "$state2/wiki-candidates.md"

out="$(CCC_STATE_DIR="$state2" bash "$ROOT/scripts/ccc-wiki-triage.sh" show CAND-010)"; rc=$?
ok "triage redacts unlabelled tokens" '[ "$rc" = 0 ] && ! grep -q "$gh_tok" <<<"$out" && ! grep -q "$aws_tok" <<<"$out" && jq -e ".candidate.redaction_applied == true" <<<"$out" >/dev/null'

out="$(CCC_STATE_DIR="$state2" bash "$ROOT/scripts/ccc-wiki-triage.sh" show CAND-011)"; rc=$?
ok "triage redacts the whole PEM block including its body" '[ "$rc" = 0 ] && ! grep -q "MIIEowIBAAKCAQEA" <<<"$out" && ! grep -q "BEGIN RSA PRIVATE KEY" <<<"$out" && jq -e ".candidate.redaction_applied == true" <<<"$out" >/dev/null'

out="$(CCC_STATE_DIR="$state2" bash "$ROOT/scripts/ccc-wiki-triage.sh" show CAND-012)"; rc=$?
ok "triage leaves ordinary prose intact" '[ "$rc" = 0 ] && grep -q "broker tunnel is documented" <<<"$out" && jq -e ".candidate.redaction_applied == false" <<<"$out" >/dev/null'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
