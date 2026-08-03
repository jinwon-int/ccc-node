#!/usr/bin/env bash
# Submit the required seoseo-ai review using the LOCAL secondary gh account.
# Mirror of approve-via-seoseo.sh for the opposite direction:
#   jinon86-authored PR -> seoseo-ai review (this helper, local credential)
#   seoseo-ai-authored PR -> jinon86 review (approve-via-seoseo.sh, Seoseo-held)
# The caller must have fresh, explicit approval for this exact credential use.
# The token is scoped to this process environment for the helper's own gh
# invocations only; it is never printed, logged, exported, or persisted, and
# the persistent active gh account is never switched.
set -euo pipefail
case "$-" in *x*) set +x ;; esac  # never trace credential-bearing commands

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit "${2:-64}"
}

if [ "${CCC_EXPLICIT_USER_APPROVAL:-0}" != "1" ]; then
  die "fresh explicit user approval is required for local seoseo-ai credential use"
fi

repo="${1:-}"
pr="${2:-}"
[ "$#" -eq 2 ] || die "usage: CCC_EXPLICIT_USER_APPROVAL=1 $0 jinwon-int/<repo> <pr-number>"
[[ "$repo" =~ ^jinwon-int/[A-Za-z0-9_.-]+$ ]] || die "repository must match jinwon-int/<repo>"
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || die "PR number must be a positive integer"

expected_author="${CCC_LOCAL_REVIEW_EXPECTED_AUTHOR:-jinon86}"
[[ "$expected_author" =~ ^[A-Za-z0-9-]+$ ]] || die "invalid expected PR author"

command -v gh >/dev/null 2>&1 || die "gh is unavailable" 69

# Scoped credential: retrieved into this process only, used via GH_TOKEN for
# the helper's gh calls, gone when the process exits. No `gh auth switch`.
token="$(gh auth token --user seoseo-ai 2>/dev/null)" ||
  die "local gh has no stored seoseo-ai account" 69
[ -n "$token" ] || die "local gh returned an empty seoseo-ai token" 69

actor="$(GH_TOKEN="$token" gh api user --jq .login)"
if [ "$actor" != "seoseo-ai" ]; then
  die "expected local review actor seoseo-ai; refusing review" 65
fi

metadata="$(GH_TOKEN="$token" gh pr view "$pr" --repo "$repo" \
  --json author,baseRefName,state \
  --jq '[.author.login, .baseRefName, .state] | @tsv')"
IFS=$'\t' read -r author base state <<<"$metadata"

if [ "$author" = "seoseo-ai" ]; then
  die "refusing self-review" 65
fi
if [ "$author" != "$expected_author" ]; then
  die "expected PR author $expected_author; refusing review for $author" 65
fi
if [ "$base" != "main" ] || [ "$state" != "OPEN" ]; then
  die "review target must be an open PR against main" 65
fi

GH_TOKEN="$token" gh pr review "$pr" --repo "$repo" --approve \
  --body "Approved after explicit operator authorization using the local seoseo-ai credential." \
  >/dev/null

GH_TOKEN="$token" gh pr view "$pr" --repo "$repo" --json reviewDecision,reviews \
  --jq '{reviewDecision, reviews: [.reviews[] | select(.author.login == "seoseo-ai") | {author: .author.login, state}]}'
