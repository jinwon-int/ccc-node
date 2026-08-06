#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/codex/skills/gh-pr-flow/scripts/approve-via-seoseo.sh"
WRAPPER="$ROOT/codex/skills/gh-pr-flow/scripts/approve-via-jinon86.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

mkdir -p "$TMP/bin" "$TMP/review-config"
printf 'test fixture; not a credential\n' >"$TMP/review-config/hosts.yml"
chmod 700 "$TMP/review-config"
chmod 600 "$TMP/review-config/hosts.yml"

cat >"$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
while [ "${1:-}" = "-o" ]; do shift 2; done
[ "${1:-}" = "seoseo" ] || { echo "unexpected SSH target" >&2; exit 90; }
shift
: >"$MOCK_SSH_MARKER"
exec "$@"
EOF

cat >"$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${GH_CONFIG_DIR:-}" = "$MOCK_EXPECTED_CONFIG" ] || {
  echo "review gh did not use the allowlisted config directory" >&2
  exit 91
}
printf '%s\n' "$*" >>"$MOCK_GH_LOG"
if [ "$1 $2" = "api user" ]; then
  printf '%s\n' "${MOCK_ACTOR:-jinon86}"
elif [ "$1" = "api" ] && [[ "$2" == repos/* ]] && [[ " $* " == *" .permissions.push "* ]]; then
  printf '%s\n' "${MOCK_PUSH:-true}"
elif [ "$1" = "api" ] && [[ "$2" == repos/* ]] && [[ " $* " == *" .default_branch "* ]]; then
  printf '%s\n' "${MOCK_DEFAULT_BRANCH:-main}"
elif [ "$1 $2" = "pr view" ] && [[ " $* " == *" author,baseRefName,state,isDraft,headRefOid,mergeable,reviewRequests,statusCheckRollup "* ]]; then
  jq -n \
    --arg author "${MOCK_AUTHOR:-seoseo-ai}" \
    --arg base "${MOCK_BASE:-main}" \
    --arg head "${MOCK_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" \
    --arg reviewer "${MOCK_REVIEWER:-jinon86}" \
    --arg conclusion "${MOCK_CONCLUSION:-SUCCESS}" \
    '{author:{login:$author},baseRefName:$base,state:"OPEN",isDraft:false,
      headRefOid:$head,mergeable:"MERGEABLE",
      reviewRequests:[{login:$reviewer}],
      statusCheckRollup:[{status:"COMPLETED",conclusion:$conclusion}]}'
elif [ "$1" = "api" ] && [ "$2" = "--method" ]; then
  : >"$MOCK_REVIEW_MARKER"
elif [ "$1 $2" = "pr view" ] && [[ " $* " == *" headRefOid,reviewDecision,reviews "* ]]; then
  jq -n --arg head "${MOCK_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" \
    '{headRefOid:$head,reviewDecision:"APPROVED",reviews:[{author:{login:"jinon86"},state:"APPROVED",commit:{oid:$head}}]}'
else
  printf 'unexpected gh call: %s\n' "$*" >&2
  exit 92
fi
EOF

cat >"$TMP/bin/stat" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "-c" ] || { echo "unexpected stat invocation" >&2; exit 93; }
case "${2:-}:${3:-}" in
  "%a:$MOCK_EXPECTED_CONFIG")
    printf '%s\n' '700'
    ;;
  "%U:%G:$MOCK_EXPECTED_CONFIG")
    printf '%s\n' 'root:root'
    ;;
  "%a:%U:%G:$MOCK_EXPECTED_CONFIG/hosts.yml")
    printf '%s\n' "${MOCK_CREDENTIAL_STAT:-600:root:root}"
    ;;
  *)
    echo "unexpected stat target" >&2
    exit 93
    ;;
esac
EOF
chmod +x "$TMP/bin/ssh" "$TMP/bin/gh" "$TMP/bin/stat"

export PATH="$TMP/bin:$PATH"
export MOCK_SSH_MARKER="$TMP/ssh.called"
export MOCK_REVIEW_MARKER="$TMP/review.called"
export MOCK_GH_LOG="$TMP/gh.calls"
export MOCK_EXPECTED_CONFIG="$TMP/review-config"
HEAD_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

run_helper() {
  CCC_EXPLICIT_USER_APPROVAL=1 \
  CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
    bash "$HELPER" --review-profile jinon86 \
      --repo jinwon-int/ccc-node --pr 948 --expected-head "$HEAD_SHA" \
      --ssh-target seoseo --operator-approved "$@"
}

run_wrapper() {
  CCC_EXPLICIT_USER_APPROVAL=1 \
  CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
    bash "$WRAPPER" --repo jinwon-int/ccc-node --pr 948 \
      --expected-head "$HEAD_SHA" --ssh-target seoseo \
      --operator-approved "$@"
}

if CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
   bash "$HELPER" --review-profile jinon86 --repo jinwon-int/ccc-node \
     --pr 948 --expected-head "$HEAD_SHA" --ssh-target seoseo \
     --operator-approved >"$TMP/no-approval.out" 2>&1; then
  bad "helper accepted a call without fresh explicit approval"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "helper contacted Seoseo before checking approval"
else
  ok
fi

if CCC_EXPLICIT_USER_APPROVAL=1 \
   CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
   bash "$HELPER" --review-profile jinon86 --repo other-owner/repo \
     --pr 948 --expected-head "$HEAD_SHA" --ssh-target seoseo \
     --operator-approved >"$TMP/bad-repo.out" 2>&1; then
  bad "helper accepted a repository outside jinwon-int"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "helper contacted Seoseo before validating repository scope"
else
  ok
fi

rm -f "$MOCK_SSH_MARKER" "$MOCK_REVIEW_MARKER"
if run_helper --dry-run >"$TMP/dry-run.out" \
   && jq -e '.ok == true and .dry_run == true and .actor == "jinon86" and .author == "seoseo-ai"' \
     "$TMP/dry-run.out" >/dev/null \
   && [ ! -e "$MOCK_REVIEW_MARKER" ]; then
  ok
else
  bad "valid jinon86 profile dry-run failed or submitted a review"
fi

rm -f "$MOCK_REVIEW_MARKER"
if run_wrapper >"$TMP/success.out" \
   && jq -e '.ok == true and .approved == true and .actor == "jinon86"' \
     "$TMP/success.out" >/dev/null \
   && [ -e "$MOCK_REVIEW_MARKER" ] \
   && grep -Fq "commit_id=$HEAD_SHA" "$MOCK_GH_LOG"; then
  ok
else
  bad "jinon86 compatibility wrapper failed exact-head approval"
fi

rm -f "$MOCK_SSH_MARKER" "$MOCK_REVIEW_MARKER"
if CCC_EXPLICIT_USER_APPROVAL=1 \
   CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
   bash "$WRAPPER" --review-profile seoseo-ai --repo jinwon-int/ccc-node \
     --pr 948 --expected-head "$HEAD_SHA" --ssh-target seoseo \
     --operator-approved >"$TMP/profile-override.out" 2>&1; then
  bad "jinon86 wrapper allowed its review profile to be overridden"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "wrapper contacted Seoseo before refusing a profile override"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_ACTOR=seoseo-ai run_helper >"$TMP/wrong-actor.out" 2>&1; then
  bad "helper accepted a remote actor other than jinon86"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a review before refusing the wrong actor"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_AUTHOR=jinon86 run_helper >"$TMP/wrong-author.out" 2>&1; then
  bad "helper accepted a jinon86-authored PR for the jinon86 profile"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a self-review before refusing the wrong author"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_HEAD=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
   run_helper >"$TMP/changed-head.out" 2>&1; then
  bad "helper accepted a changed PR head"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a review before refusing changed head"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_REVIEWER=seoseo-ai run_helper >"$TMP/not-requested.out" 2>&1; then
  bad "helper approved without a jinon86 review request"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted an unrequested review before refusing"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_CONCLUSION=FAILURE run_helper >"$TMP/failed-check.out" 2>&1; then
  bad "helper accepted a failed exact-head check"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a review before refusing failed checks"
else
  ok
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_CREDENTIAL_STAT=644:root:root \
   run_helper >"$TMP/unsafe-mode.out" 2>&1; then
  bad "helper accepted a non-owner-only credential file"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a review with an unsafe credential file"
else
  ok
fi

if grep -Fq 'auth token' "$MOCK_GH_LOG"; then
  bad "helper extracted the remote credential"
else
  ok
fi

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
