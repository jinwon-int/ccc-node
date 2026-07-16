#!/usr/bin/env bash
# Fail-closed squash merge through Seoseo's existing jinon86 gh session.
# The token never leaves Seoseo; this helper sends only repository metadata.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: merge-via-seoseo.sh --repo OWNER/REPO --pr NUMBER \
  --expected-head 40_HEX_SHA --operator-approved [--ssh-target HOST] [--dry-run]
EOF
}

repo=""
pr=""
expected_head=""
ssh_target="${CCC_SEOSEO_SSH_TARGET:-seoseo}"
expected_actor="${CCC_SEOSEO_MERGE_ACTOR:-jinon86}"
approved=0
dry_run=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --pr) pr="${2:-}"; shift 2 ;;
    --expected-head) expected_head="${2:-}"; shift 2 ;;
    --ssh-target) ssh_target="${2:-}"; shift 2 ;;
    --operator-approved) approved=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ "$approved" -eq 1 ] || {
  echo "refusing merge: --operator-approved is required for this exact PR" >&2
  exit 2
}
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
  echo "invalid --repo: expected OWNER/REPO" >&2
  exit 2
}
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || {
  echo "invalid --pr: expected a positive integer" >&2
  exit 2
}
[[ "$expected_head" =~ ^[0-9a-fA-F]{40}$ ]] || {
  echo "invalid --expected-head: expected a full 40-character SHA" >&2
  exit 2
}
[[ "$ssh_target" =~ ^[A-Za-z0-9_.@-]+$ ]] || {
  echo "invalid --ssh-target" >&2
  exit 2
}
[[ "$expected_actor" =~ ^[A-Za-z0-9-]+$ ]] || {
  echo "invalid expected merge actor" >&2
  exit 2
}
command -v ssh >/dev/null 2>&1 || { echo "ssh is required" >&2; exit 2; }

ssh -o BatchMode=yes -o ConnectTimeout="${CCC_SEOSEO_SSH_TIMEOUT:-8}" \
  "$ssh_target" bash -s -- \
  "$repo" "$pr" "${expected_head,,}" "$expected_actor" "$dry_run" <<'REMOTE'
set -euo pipefail

repo="$1"
pr="$2"
expected_head="$3"
expected_actor="$4"
dry_run="$5"

command -v gh >/dev/null 2>&1 || { echo "Seoseo gh is unavailable" >&2; exit 3; }
command -v jq >/dev/null 2>&1 || { echo "Seoseo jq is unavailable" >&2; exit 3; }

actor="$(gh api user --jq .login)"
[ "$actor" = "$expected_actor" ] || {
  echo "refusing merge: remote actor is not $expected_actor" >&2
  exit 3
}

pr_json="$(gh pr view "$pr" --repo "$repo" \
  --json state,isDraft,baseRefName,headRefOid,mergeable,mergeStateStatus,statusCheckRollup)"

[ "$(jq -r '.state' <<<"$pr_json")" = "OPEN" ] || {
  echo "refusing merge: PR is not open" >&2
  exit 4
}
[ "$(jq -r '.isDraft' <<<"$pr_json")" = "false" ] || {
  echo "refusing merge: PR is still draft" >&2
  exit 4
}
[ "$(jq -r '.baseRefName' <<<"$pr_json")" = "main" ] || {
  echo "refusing merge: base branch is not main" >&2
  exit 4
}
actual_head="$(jq -r '.headRefOid | ascii_downcase' <<<"$pr_json")"
[ "$actual_head" = "$expected_head" ] || {
  echo "refusing merge: PR head changed" >&2
  exit 4
}
[ "$(jq -r '.mergeable' <<<"$pr_json")" = "MERGEABLE" ] || {
  echo "refusing merge: PR is not mergeable" >&2
  exit 4
}
[ "$(jq -r '.mergeStateStatus' <<<"$pr_json")" = "CLEAN" ] || {
  echo "refusing merge: merge state is not CLEAN" >&2
  exit 4
}

check_count="$(jq '.statusCheckRollup | length' <<<"$pr_json")"
bad_checks="$(jq '[.statusCheckRollup[]? | select(
  if ((.state? // "") | type) == "string" and (.state? // "") != "" then
    .state != "SUCCESS"
  else
    (.status != "COMPLETED") or
    ((.conclusion // "") as $c | ($c != "SUCCESS" and $c != "NEUTRAL" and $c != "SKIPPED"))
  end
)] | length' <<<"$pr_json")"
[ "$bad_checks" -eq 0 ] || {
  echo "refusing merge: pending or unsuccessful checks exist" >&2
  exit 4
}

if [ "$dry_run" -eq 1 ]; then
  jq -n --arg repo "$repo" --argjson pr "$pr" --arg actor "$actor" \
    --arg head "$actual_head" --argjson checks "$check_count" \
    '{ok:true,dry_run:true,repo:$repo,pr:$pr,actor:$actor,head:$head,check_count:$checks}'
  exit 0
fi

result="$(gh api --method PUT "repos/$repo/pulls/$pr/merge" \
  -f merge_method=squash -f "sha=$expected_head")"
jq -e '.merged == true' >/dev/null <<<"$result" || {
  echo "merge API did not confirm success" >&2
  jq '{merged,message}' <<<"$result" >&2
  exit 5
}

jq -n --arg repo "$repo" --argjson pr "$pr" --arg actor "$actor" \
  --arg head "$actual_head" --arg merge_sha "$(jq -r '.sha' <<<"$result")" \
  --argjson checks "$check_count" \
  '{ok:true,merged:true,repo:$repo,pr:$pr,actor:$actor,head:$head,merge_sha:$merge_sha,check_count:$checks}'
REMOTE
