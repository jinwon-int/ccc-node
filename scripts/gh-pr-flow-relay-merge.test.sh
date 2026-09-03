#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/skills/gh-pr-flow/merge-via-relay.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL + 1)); }

mkdir -p "$TMP/bin"
cat > "$TMP/bin/ssh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    *) break ;;
  esac
done
shift
exec "$@"
SH
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = api ] && [ "${2:-}" = user ]; then
  printf '%s\n' "${FAKE_ACTOR:-jinon86}"
elif [ "${1:-}" = api ] && [[ "${2:-}" == repos/* ]]; then
  printf '%s\n' "${FAKE_DEFAULT_BRANCH:-main}"
elif [ "${1:-}" = pr ] && [ "${2:-}" = view ]; then
  jq -n \
    --arg head "${FAKE_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" \
    --arg state "${FAKE_STATE:-OPEN}" \
    --arg base "${FAKE_BASE:-main}" \
    --arg mergeable "${FAKE_MERGEABLE:-MERGEABLE}" \
    --arg merge_state "${FAKE_MERGE_STATE:-CLEAN}" \
    --argjson draft "${FAKE_DRAFT:-false}" \
    --argjson checks "${FAKE_CHECKS:-[]}" \
    '{state:$state,isDraft:$draft,baseRefName:$base,headRefOid:$head,mergeable:$mergeable,mergeStateStatus:$merge_state,statusCheckRollup:$checks}'
elif [ "${1:-}" = api ] && [ "${2:-}" = --method ]; then
  : > "${FAKE_MERGE_MARKER:?}"
  printf '%s\n' '{"merged":true,"message":"ok","sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
else
  echo "unexpected fake gh invocation: $*" >&2
  exit 90
fi
SH
chmod +x "$TMP/bin/ssh" "$TMP/bin/gh"

export PATH="$TMP/bin:$PATH"
export FAKE_MERGE_MARKER="$TMP/merged"
HEAD_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

if bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 --expected-head "$HEAD_SHA" \
  >"$TMP/out" 2>"$TMP/err"; then
  fail "merge succeeded without explicit approval"
elif grep -q -- '--operator-approved is required' "$TMP/err"; then
  pass
else
  fail "missing approval failure was not explicit"
fi

if bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 \
  --expected-head cccccccccccccccccccccccccccccccccccccccc \
  --operator-approved --dry-run >"$TMP/out" 2>"$TMP/err"; then
  fail "changed head was accepted"
elif grep -q 'PR head changed' "$TMP/err"; then
  pass
else
  fail "changed-head failure was not explicit"
fi

FAKE_CHECKS='[{"status":"COMPLETED","conclusion":"FAILURE"}]' \
  bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 --expected-head "$HEAD_SHA" \
  --operator-approved --dry-run >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'unsuccessful checks' "$TMP/err"; then
  pass
else
  fail "failed checks were not rejected"
fi

FAKE_CHECKS='[{"state":"SUCCESS","context":"legacy-status"}]' \
  bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 --expected-head "$HEAD_SHA" \
  --operator-approved --dry-run >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && jq -e '.check_count == 1' "$TMP/out" >/dev/null; then
  pass
else
  fail "successful legacy status context was rejected"
fi

rm -f "$FAKE_MERGE_MARKER"
if bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 --expected-head "$HEAD_SHA" \
  --operator-approved --dry-run >"$TMP/out"; then
  jq -e '.ok == true and .dry_run == true and .actor == "jinon86" and .check_count == 0' \
    "$TMP/out" >/dev/null && [ ! -e "$FAKE_MERGE_MARKER" ] && pass \
    || fail "dry-run output or mutation guard invalid"
else
  fail "valid dry-run failed"
fi

rm -f "$FAKE_MERGE_MARKER"
if bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 --expected-head "$HEAD_SHA" \
  --operator-approved >"$TMP/out"; then
  jq -e '.ok == true and .merged == true and .merge_sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"' \
    "$TMP/out" >/dev/null && [ -e "$FAKE_MERGE_MARKER" ] && pass \
    || fail "merge output or API invocation invalid"
else
  fail "valid merge failed"
fi

# Base guard is the repository's own default branch, not the literal "main".
rm -f "$FAKE_MERGE_MARKER"
if FAKE_DEFAULT_BRANCH=master FAKE_BASE=master \
  bash "$SCRIPT" --repo jinwon-int/seoyoon-family-wiki --pr 3304 --expected-head "$HEAD_SHA" \
  --operator-approved --dry-run >"$TMP/out" 2>"$TMP/err"; then
  pass
else
  fail "master-based repository default branch was rejected"
fi

FAKE_DEFAULT_BRANCH=main FAKE_BASE=release/2026-08 \
  bash "$SCRIPT" --repo jinwon-int/ccc-node --pr 1 --expected-head "$HEAD_SHA" \
  --operator-approved --dry-run >"$TMP/out" 2>"$TMP/err" && rc=0 || rc=$?
if [ "$rc" -ne 0 ] && grep -q 'base branch is not the repository default branch' "$TMP/err"; then
  pass
else
  fail "non-default base branch was not rejected"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
