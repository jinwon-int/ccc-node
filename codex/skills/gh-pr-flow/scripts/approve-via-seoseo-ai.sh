#!/usr/bin/env bash
# Submit an exact-head approval through Seoseo's isolated seoseo-ai gh config.
# The credential remains on Seoseo and is never printed or copied.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: approve-via-seoseo-ai.sh --repo jinwon-int/REPO --pr NUMBER \
  --expected-head 40_HEX_SHA --operator-approved \
  [--ssh-target HOST] [--dry-run]
EOF
}

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit "${2:-64}"
}

repo=""
pr=""
expected_head=""
ssh_target="seoseo"
approved=0
dry_run=0
review_config="${CCC_SEOSEO_AI_GH_CONFIG_DIR:-/root/.config/gh-seoseo-ai}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --pr) pr="${2:-}"; shift 2 ;;
    --expected-head) expected_head="${2:-}"; shift 2 ;;
    --ssh-target) ssh_target="${2:-}"; shift 2 ;;
    --operator-approved) approved=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "${CCC_EXPLICIT_USER_APPROVAL:-0}" = "1" ] \
  || die "fresh explicit user approval is required"
[ "$approved" -eq 1 ] \
  || die "--operator-approved is required for this exact PR"
[[ "$repo" =~ ^jinwon-int/[A-Za-z0-9_.-]+$ ]] \
  || die "repository must match jinwon-int/REPO"
[[ "$pr" =~ ^[1-9][0-9]*$ ]] \
  || die "PR number must be a positive integer"
[[ "$expected_head" =~ ^[0-9a-fA-F]{40}$ ]] \
  || die "expected head must be a full 40-character SHA"
[[ "$ssh_target" =~ ^[A-Za-z0-9_.@-]+$ ]] \
  || die "invalid SSH target"
[[ "$review_config" =~ ^/[A-Za-z0-9_./-]+$ ]] \
  || die "invalid remote gh config directory"
[[ "/$review_config/" != *"/../"* ]] \
  || die "remote gh config directory must not contain parent traversal"
command -v ssh >/dev/null 2>&1 || die "ssh is unavailable" 69

ssh -o BatchMode=yes -o ConnectTimeout=8 "$ssh_target" bash -s -- \
  "$repo" "$pr" "${expected_head,,}" "$review_config" "$dry_run" <<'REMOTE'
set -euo pipefail

repo="$1"
pr="$2"
expected_head="$3"
review_config="$4"
dry_run="$5"
credential_file="$review_config/hosts.yml"

command -v gh >/dev/null 2>&1 \
  || { echo "ERROR: Seoseo gh is unavailable" >&2; exit 69; }
command -v jq >/dev/null 2>&1 \
  || { echo "ERROR: Seoseo jq is unavailable" >&2; exit 69; }
[ -d "$review_config" ] && [ ! -L "$review_config" ] \
  || { echo "ERROR: isolated gh config directory is unsafe" >&2; exit 65; }
[ -f "$credential_file" ] && [ ! -L "$credential_file" ] \
  || { echo "ERROR: isolated gh credential file is unsafe" >&2; exit 65; }
dir_mode="$(stat -c '%a' "$review_config")"
[ "$(stat -c '%U:%G' "$review_config")" = "root:root" ] \
  && (( (8#$dir_mode & 8#022) == 0 )) \
  || { echo "ERROR: isolated gh config directory owner or write mode is unsafe" >&2; exit 65; }
[ "$(stat -c '%a:%U:%G' "$credential_file")" = "600:root:root" ] \
  || { echo "ERROR: isolated gh credential file owner or mode is unsafe" >&2; exit 65; }

review_gh() {
  GH_CONFIG_DIR="$review_config" gh "$@"
}

actor="$(review_gh api user --jq .login)"
[ "$actor" = "seoseo-ai" ] \
  || { echo "ERROR: expected review actor seoseo-ai" >&2; exit 65; }
[ "$(review_gh api "repos/$repo" --jq .permissions.push)" = "true" ] \
  || { echo "ERROR: seoseo-ai lacks repository write permission" >&2; exit 65; }

pr_json="$(review_gh pr view "$pr" --repo "$repo" \
  --json author,baseRefName,state,isDraft,headRefOid,mergeable,reviewRequests,statusCheckRollup)"

[ "$(jq -r '.author.login' <<<"$pr_json")" = "jinon86" ] \
  || { echo "ERROR: expected PR author jinon86" >&2; exit 65; }
[ "$(jq -r '.baseRefName' <<<"$pr_json")" = "main" ] \
  || { echo "ERROR: review target base is not main" >&2; exit 65; }
[ "$(jq -r '.state' <<<"$pr_json")" = "OPEN" ] \
  || { echo "ERROR: review target is not open" >&2; exit 65; }
[ "$(jq -r '.isDraft' <<<"$pr_json")" = "false" ] \
  || { echo "ERROR: review target is still draft" >&2; exit 65; }
[ "$(jq -r '.headRefOid | ascii_downcase' <<<"$pr_json")" = "$expected_head" ] \
  || { echo "ERROR: PR head changed" >&2; exit 65; }
[ "$(jq -r '.mergeable' <<<"$pr_json")" = "MERGEABLE" ] \
  || { echo "ERROR: PR is not mergeable" >&2; exit 65; }
[ "$(jq -r '([.reviewRequests[].login] | index("seoseo-ai")) != null' <<<"$pr_json")" = "true" ] \
  || { echo "ERROR: seoseo-ai is not a requested reviewer" >&2; exit 65; }

check_count="$(jq '.statusCheckRollup | length' <<<"$pr_json")"
[ "$check_count" -gt 0 ] \
  || { echo "ERROR: GitHub reported no checks for the exact head" >&2; exit 65; }
bad_checks="$(jq '[.statusCheckRollup[]? | select(
  if ((.state? // "") | type) == "string" and (.state? // "") != "" then
    .state != "SUCCESS"
  else
    (.status != "COMPLETED") or
    ((.conclusion // "") as $c |
      ($c != "SUCCESS" and $c != "NEUTRAL" and $c != "SKIPPED"))
  end
)] | length' <<<"$pr_json")"
[ "$bad_checks" -eq 0 ] \
  || { echo "ERROR: pending or unsuccessful checks exist" >&2; exit 65; }

if [ "$dry_run" -eq 1 ]; then
  jq -n --arg repo "$repo" --argjson pr "$pr" --arg actor "$actor" \
    --arg head "$expected_head" --argjson checks "$check_count" \
    '{ok:true,dry_run:true,repo:$repo,pr:$pr,actor:$actor,head:$head,check_count:$checks}'
  exit 0
fi

review_gh api --method POST "repos/$repo/pulls/$pr/reviews" \
  -f event=APPROVE \
  -f "commit_id=$expected_head" \
  -f body='Approved after exact-head validation and fresh operator authorization using the Seoseo-held seoseo-ai credential.' \
  >/dev/null

after="$(review_gh pr view "$pr" --repo "$repo" \
  --json headRefOid,reviewDecision,reviews)"
[ "$(jq -r '.headRefOid | ascii_downcase' <<<"$after")" = "$expected_head" ] \
  || { echo "ERROR: PR head changed during review" >&2; exit 65; }
[ "$(jq -r '.reviewDecision' <<<"$after")" = "APPROVED" ] \
  || { echo "ERROR: GitHub did not record an approving review" >&2; exit 65; }

jq -n --arg repo "$repo" --argjson pr "$pr" --arg actor "$actor" \
  --arg head "$expected_head" --argjson checks "$check_count" \
  '{ok:true,approved:true,repo:$repo,pr:$pr,actor:$actor,head:$head,check_count:$checks}'
REMOTE
