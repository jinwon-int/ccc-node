#!/usr/bin/env bash
# Coverage for claude/skills/gh-pr-flow/approve-as-seoseo-ai.sh — the local
# secondary-account review helper (jinon86-authored PR -> seoseo-ai review).
# It had no suite at all, so its guards were unverified; the base-branch guard
# in particular carried the same hardcoded-"main" defect fixed here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/claude/skills/gh-pr-flow/approve-as-seoseo-ai.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

mkdir -p "$TMP/bin"

cat >"$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$MOCK_GH_LOG"
if [ "${1:-} ${2:-}" = "auth token" ]; then
  printf '%s\n' "${MOCK_TOKEN:-fake-token-value}"
elif [ "${1:-} ${2:-}" = "api user" ]; then
  printf '%s\n' "${MOCK_ACTOR:-seoseo-ai}"
elif [ "${1:-}" = "api" ] && [[ "${2:-}" == repos/* ]]; then
  printf '%s\n' "${MOCK_DEFAULT_BRANCH:-main}"
elif [ "${1:-} ${2:-}" = "pr view" ] && [[ " $* " == *" author,baseRefName,state "* ]]; then
  printf '%s\t%s\t%s\n' \
    "${MOCK_AUTHOR:-jinon86}" "${MOCK_BASE:-main}" "${MOCK_STATE:-OPEN}"
elif [ "${1:-} ${2:-}" = "pr review" ]; then
  : >"$MOCK_REVIEW_MARKER"
elif [ "${1:-} ${2:-}" = "pr view" ] && [[ " $* " == *" reviewDecision,reviews "* ]]; then
  printf '%s\n' '{"reviewDecision":"APPROVED","reviews":[{"author":"seoseo-ai","state":"APPROVED"}]}'
else
  printf 'unexpected gh call: %s\n' "$*" >&2
  exit 92
fi
EOF
chmod +x "$TMP/bin/gh"

export PATH="$TMP/bin:$PATH"
export MOCK_REVIEW_MARKER="$TMP/review.called"
export MOCK_GH_LOG="$TMP/gh.calls"

run() { CCC_EXPLICIT_USER_APPROVAL=1 bash "$HELPER" "$@"; }

# --- approval gate -----------------------------------------------------------
if bash "$HELPER" jinwon-int/ccc-node 535 >"$TMP/no-approval.out" 2>&1; then
  bad "helper accepted a call without fresh explicit approval"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper reviewed before checking approval"
else
  ok
fi

# --- repository scope --------------------------------------------------------
rm -f "$MOCK_REVIEW_MARKER"
if run other-owner/repo 535 >"$TMP/bad-repo.out" 2>&1; then
  bad "helper accepted a repository outside jinwon-int"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper reviewed before validating repository scope"
else
  ok
fi

# --- happy path --------------------------------------------------------------
rm -f "$MOCK_REVIEW_MARKER"
if run jinwon-int/ccc-node 535 >"$TMP/success.out" 2>&1 && [ -e "$MOCK_REVIEW_MARKER" ]; then
  ok
else
  bad "approved review path failed"
fi

# The token is fetched deliberately here, unlike the Seoseo-held helper, so the
# assertion is that its VALUE never reaches a log or an argv, not that
# `gh auth token` is unused.
if grep -Fq 'fake-token-value' "$MOCK_GH_LOG"; then
  bad "helper leaked the credential value into a gh argument"
else
  ok
fi

# --- wrong local actor -------------------------------------------------------
rm -f "$MOCK_REVIEW_MARKER"
if MOCK_ACTOR=jinon86 run jinwon-int/ccc-node 535 >"$TMP/wrong-actor.out" 2>&1; then
  bad "helper accepted a local actor other than seoseo-ai"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper reviewed before refusing the wrong actor"
else
  ok
fi

# --- self-review -------------------------------------------------------------
rm -f "$MOCK_REVIEW_MARKER"
if MOCK_AUTHOR=seoseo-ai run jinwon-int/ccc-node 535 >"$TMP/self-review.out" 2>&1; then
  bad "helper allowed a self-review"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a self-review before refusing"
else
  ok
fi

# --- unexpected PR author ----------------------------------------------------
rm -f "$MOCK_REVIEW_MARKER"
if MOCK_AUTHOR=someone-else run jinwon-int/ccc-node 535 >"$TMP/other-author.out" 2>&1; then
  bad "helper reviewed a PR from an unexpected author"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper reviewed before refusing an unexpected author"
else
  ok
fi

# --- default-branch guard ----------------------------------------------------
# The guard is "PR targets the repository's own default branch", not "the branch
# is named main". Hardcoding main refused every PR in a master-based repo.
rm -f "$MOCK_REVIEW_MARKER"
if MOCK_DEFAULT_BRANCH=master MOCK_BASE=master \
   run jinwon-int/seoyoon-family-wiki 3304 >"$TMP/master-repo.out" 2>&1 \
   && [ -e "$MOCK_REVIEW_MARKER" ]; then
  ok
else
  bad "helper refused a PR against a master-based repository's default branch"
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_DEFAULT_BRANCH=main MOCK_BASE=release/2026-08 \
   run jinwon-int/ccc-node 535 >"$TMP/wrong-base.out" 2>&1; then
  bad "helper approved a PR whose base is not the default branch"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper reviewed before refusing a non-default base"
else
  ok
fi

# --- closed PR ---------------------------------------------------------------
rm -f "$MOCK_REVIEW_MARKER"
if MOCK_STATE=CLOSED run jinwon-int/ccc-node 535 >"$TMP/closed.out" 2>&1; then
  bad "helper reviewed a closed PR"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper reviewed before refusing a closed PR"
else
  ok
fi

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
