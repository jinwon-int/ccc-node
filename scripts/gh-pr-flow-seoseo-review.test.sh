#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/claude/skills/gh-pr-flow/approve-via-seoseo.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

mkdir -p "$TMP/bin"

apply_mock_files() {
  cat >"$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
while [ "${1:-}" = "-o" ]; do shift 2; done
[ "${1:-}" = "seoseo" ] || { echo "unexpected ssh host" >&2; exit 90; }
shift
: >"$MOCK_SSH_MARKER"
exec "$@"
EOF

  cat >"$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1 $2 $3 $4" = "auth token --user jinon86" ]; then
  printf '%s\n' 'FAKE_TEST_TOKEN_DO_NOT_USE'
elif [ "$1 $2" = "pr view" ] && [[ " $* " == *" author,baseRefName,state,reviewRequests "* ]]; then
  printf '%s\tmain\tOPEN\t%s\n' "${MOCK_AUTHOR:-seoseo-ai}" "${MOCK_REQUESTED:-true}"
elif [ "$1 $2" = "pr review" ]; then
  [ "${GH_TOKEN:-}" = "FAKE_TEST_TOKEN_DO_NOT_USE" ] || exit 91
  : >"$MOCK_REVIEW_MARKER"
elif [ "$1 $2" = "pr view" ] && [[ " $* " == *" reviewDecision,reviews "* ]]; then
  [ "${GH_TOKEN:-}" = "FAKE_TEST_TOKEN_DO_NOT_USE" ] || exit 91
  printf '%s\n' '{"reviewDecision":"APPROVED","reviews":[{"author":"jinon86","state":"APPROVED"}]}'
else
  printf 'unexpected gh call: %s\n' "$*" >&2
  exit 92
fi
EOF
  chmod +x "$TMP/bin/ssh" "$TMP/bin/gh"
}

apply_mock_files
export PATH="$TMP/bin:$PATH"
export MOCK_SSH_MARKER="$TMP/ssh.called"
export MOCK_REVIEW_MARKER="$TMP/review.called"

if "$HELPER" jinwon-int/ccc-node 535 >"$TMP/no-approval.out" 2>&1; then
  bad "helper accepted a call without fresh explicit approval"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "helper contacted Seoseo before checking approval"
else
  ok
fi

if CCC_EXPLICIT_USER_APPROVAL=1 "$HELPER" other-owner/repo 535 >"$TMP/bad-repo.out" 2>&1; then
  bad "helper accepted a repository outside jinwon-int"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "helper contacted Seoseo before validating repository scope"
else
  ok
fi

if CCC_EXPLICIT_USER_APPROVAL=1 "$HELPER" jinwon-int/ccc-node 535 >"$TMP/success.out" 2>&1 \
   && [ -e "$MOCK_SSH_MARKER" ] && [ -e "$MOCK_REVIEW_MARKER" ]; then
  ok
else
  bad "approved review path failed"
fi
if grep -Fq 'FAKE_TEST_TOKEN_DO_NOT_USE' "$TMP/success.out"; then
  bad "helper leaked the credential value to output"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_AUTHOR=jinon86 CCC_EXPLICIT_USER_APPROVAL=1 \
   "$HELPER" jinwon-int/ccc-node 535 >"$TMP/self-review.out" 2>&1; then
  bad "helper allowed a self-review"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a self-review before refusing"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_REQUESTED=false CCC_EXPLICIT_USER_APPROVAL=1 \
   "$HELPER" jinwon-int/ccc-node 535 >"$TMP/not-requested.out" 2>&1; then
  bad "helper approved without a jinon86 review request"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted an unrequested review before refusing"
else
  ok
fi

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
