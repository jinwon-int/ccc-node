#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/codex/skills/gh-pr-flow/scripts/approve-via-seoseo-ai.sh"
TMP="$(mktemp -d)"
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
  echo "review gh did not use the isolated config directory" >&2
  exit 91
}
printf '%s\n' "$*" >>"$MOCK_GH_LOG"
if [ "$1 $2" = "api user" ]; then
  printf '%s\n' "${MOCK_ACTOR:-seoseo-ai}"
elif [ "$1" = "api" ] && [[ "$2" == repos/* ]] && [[ " $* " == *" .permissions.push "* ]]; then
  printf '%s\n' "${MOCK_PUSH:-true}"
elif [ "$1 $2" = "pr view" ] && [[ " $* " == *" author,baseRefName,state,isDraft,headRefOid,mergeable,reviewRequests,statusCheckRollup "* ]]; then
  jq -n \
    --arg author "${MOCK_AUTHOR:-jinon86}" \
    --arg head "${MOCK_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" \
    --arg reviewer "${MOCK_REVIEWER:-seoseo-ai}" \
    '{author:{login:$author},baseRefName:"main",state:"OPEN",isDraft:false,
      headRefOid:$head,mergeable:"MERGEABLE",
      reviewRequests:[{login:$reviewer}],
      statusCheckRollup:[{status:"COMPLETED",conclusion:"SUCCESS"}]}'
elif [ "$1" = "api" ] && [ "$2" = "--method" ]; then
  : >"$MOCK_REVIEW_MARKER"
elif [ "$1 $2" = "pr view" ] && [[ " $* " == *" headRefOid,reviewDecision,reviews "* ]]; then
  jq -n --arg head "${MOCK_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" \
    '{headRefOid:$head,reviewDecision:"APPROVED",reviews:[{author:{login:"seoseo-ai"},state:"APPROVED"}]}'
else
  printf 'unexpected gh call: %s\n' "$*" >&2
  exit 92
fi
EOF
chmod +x "$TMP/bin/ssh" "$TMP/bin/gh"

export PATH="$TMP/bin:$PATH"
export MOCK_SSH_MARKER="$TMP/ssh.called"
export MOCK_REVIEW_MARKER="$TMP/review.called"
export MOCK_GH_LOG="$TMP/gh.calls"
export MOCK_EXPECTED_CONFIG="$TMP/review-config"
HEAD_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

run_helper() {
  CCC_EXPLICIT_USER_APPROVAL=1 \
  CCC_SEOSEO_AI_GH_CONFIG_DIR="$TMP/review-config" \
    bash "$HELPER" --repo jinwon-int/ccc-node --pr 658 \
      --expected-head "$HEAD_SHA" --ssh-target seoseo \
      --operator-approved "$@"
}

if CCC_SEOSEO_AI_GH_CONFIG_DIR="$TMP/review-config" \
   bash "$HELPER" --repo jinwon-int/ccc-node --pr 658 \
     --expected-head "$HEAD_SHA" --ssh-target seoseo \
     --operator-approved >"$TMP/no-approval.out" 2>&1; then
  bad "helper accepted a call without fresh explicit approval"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "helper contacted Seoseo before checking approval"
else
  ok
fi

if CCC_EXPLICIT_USER_APPROVAL=1 \
   CCC_SEOSEO_AI_GH_CONFIG_DIR="$TMP/review-config" \
   bash "$HELPER" --repo other-owner/repo --pr 658 \
     --expected-head "$HEAD_SHA" --ssh-target seoseo \
     --operator-approved >"$TMP/bad-repo.out" 2>&1; then
  bad "helper accepted a repository outside jinwon-int"
elif [ -e "$MOCK_SSH_MARKER" ]; then
  bad "helper contacted Seoseo before validating repository scope"
else
  ok
fi

rm -f "$MOCK_SSH_MARKER" "$MOCK_REVIEW_MARKER"
if run_helper --dry-run >"$TMP/dry-run.out" \
   && jq -e '.ok == true and .dry_run == true and .actor == "seoseo-ai"' \
     "$TMP/dry-run.out" >/dev/null \
   && [ ! -e "$MOCK_REVIEW_MARKER" ]; then
  ok
else
  bad "valid dry-run failed or submitted a review"
fi

rm -f "$MOCK_REVIEW_MARKER"
if run_helper >"$TMP/success.out" \
   && jq -e '.ok == true and .approved == true and .actor == "seoseo-ai"' \
     "$TMP/success.out" >/dev/null \
   && [ -e "$MOCK_REVIEW_MARKER" ]; then
  ok
else
  bad "approved review path failed"
fi

rm -f "$MOCK_REVIEW_MARKER"
if MOCK_ACTOR=jinon86 run_helper >"$TMP/wrong-actor.out" 2>&1; then
  bad "helper accepted a remote actor other than seoseo-ai"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a review before refusing the wrong actor"
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

chmod 644 "$TMP/review-config/hosts.yml"
rm -f "$MOCK_REVIEW_MARKER"
if run_helper >"$TMP/unsafe-mode.out" 2>&1; then
  bad "helper accepted a non-owner-only credential file"
elif [ -e "$MOCK_REVIEW_MARKER" ]; then
  bad "helper submitted a review with an unsafe credential file"
else
  ok
fi
chmod 600 "$TMP/review-config/hosts.yml"

if grep -Fq 'auth token' "$MOCK_GH_LOG"; then
  bad "helper extracted the remote credential"
else
  ok
fi

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
