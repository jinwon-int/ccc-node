#!/usr/bin/env bash
# Submit the required jinon86 review using the credential held on Seoseo.
# The caller must have fresh, explicit approval for this exact credential use.
set -euo pipefail

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit "${2:-64}"
}

if [ "${CCC_EXPLICIT_USER_APPROVAL:-0}" != "1" ]; then
  die "fresh explicit user approval is required for Seoseo-held jinon86 credential use"
fi

repo="${1:-}"
pr="${2:-}"
[ "$#" -eq 2 ] || die "usage: CCC_EXPLICIT_USER_APPROVAL=1 $0 jinwon-int/<repo> <pr-number>"
[[ "$repo" =~ ^jinwon-int/[A-Za-z0-9_.-]+$ ]] || die "repository must match jinwon-int/<repo>"
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || die "PR number must be a positive integer"

ssh -o BatchMode=yes -o ConnectTimeout=8 seoseo bash -s -- "$repo" "$pr" <<'REMOTE'
set -euo pipefail
repo="$1"
pr="$2"

token="$(gh auth token --user jinon86)"
if [ -z "$token" ]; then
  echo "ERROR: Seoseo-held jinon86 credential is unavailable" >&2
  exit 69
fi
trap 'unset token' EXIT

metadata="$(GH_TOKEN="$token" gh pr view "$pr" --repo "$repo" \
  --json author,baseRefName,state,reviewRequests \
  --jq '[.author.login, .baseRefName, .state, (([.reviewRequests[].login] | index("jinon86")) != null)] | @tsv')"
IFS=$'\t' read -r author base state requested <<<"$metadata"

if [ "$author" = "jinon86" ]; then
  echo "ERROR: refusing self-review" >&2
  exit 65
fi
if [ "$author" != "seoseo-ai" ]; then
  echo "ERROR: expected PR author seoseo-ai; refusing review for $author" >&2
  exit 65
fi
if [ "$base" != "main" ] || [ "$state" != "OPEN" ]; then
  echo "ERROR: review target must be an open PR against main" >&2
  exit 65
fi
if [ "$requested" != "true" ]; then
  echo "ERROR: jinon86 is not a requested reviewer" >&2
  exit 65
fi

GH_TOKEN="$token" gh pr review "$pr" --repo "$repo" --approve \
  --body "Approved after explicit operator authorization using the Seoseo-held jinon86 credential." \
  >/dev/null

GH_TOKEN="$token" gh pr view "$pr" --repo "$repo" --json reviewDecision,reviews \
  --jq '{reviewDecision, reviews: [.reviews[] | select(.author.login == "jinon86") | {author: .author.login, state}]}'
REMOTE
