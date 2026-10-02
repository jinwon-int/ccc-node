#!/usr/bin/env bash
# nudge-hook-coverage — which repositories of an organisation lack the webhook
# that feeds the external-wait nudge relay (#1222/#1229)?
#
# The nudge is registered per repository (an org-level hook needs a token scope
# the fleet does not hold), so every new repository needs one more hook — a
# rule that otherwise lives in people's memory. 2026-10-02 found 6 of 42
# repositories without it, three of them created weeks earlier. This script is
# the periodic check: read-only against GitHub, prints one line per repository,
# and exits 10 when a repository that has CI workflows has no matching hook.
#
# Run it where an admin-scoped `gh` session exists (hook listing needs admin on
# the repository); pass GH_CONFIG_DIR through the environment as usual. It
# never prints hook secrets (the API masks them) and never creates hooks —
# registration stays a separate, approved step.
#
# Usage:
#   nudge-hook-coverage.sh --org <org> --url-pattern <substring-or-regex>
#                          [--comment <owner/repo#issue>] [--always-comment]
#                          [--include-no-ci] [--json]
# Exit: 0 full coverage · 10 gaps found · 2 usage · 3 gh/API error.
# --comment posts a body-free summary (repo names, hook ids, created dates)
# to the issue only when gaps are found (or always with --always-comment).
set -uo pipefail

ORG=""; PATTERN=""; COMMENT=""; ALWAYS=0; INCLUDE_NO_CI=0; JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="${2:-}"; shift 2 ;;
    --url-pattern) PATTERN="${2:-}"; shift 2 ;;
    --comment) COMMENT="${2:-}"; shift 2 ;;
    --always-comment) ALWAYS=1; shift ;;
    --include-no-ci) INCLUDE_NO_CI=1; shift ;;
    --json) JSON=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "nudge-hook-coverage: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ORG" ] && [ -n "$PATTERN" ] || { echo "nudge-hook-coverage: --org and --url-pattern are required" >&2; exit 2; }
if [ -n "$COMMENT" ]; then
  case "$COMMENT" in */*#[0-9]*) ;; *) echo "nudge-hook-coverage: --comment must look like owner/repo#123" >&2; exit 2 ;; esac
fi
command -v gh >/dev/null 2>&1 || { echo "nudge-hook-coverage: gh is unavailable" >&2; exit 3; }
command -v jq >/dev/null 2>&1 || { echo "nudge-hook-coverage: jq is unavailable" >&2; exit 3; }

repos="$(gh repo list "$ORG" --limit 200 --no-archived --json name,createdAt,pushedAt,visibility \
  --jq 'sort_by(.name)[]|"\(.name) \(.createdAt[0:10]) \(.pushedAt[0:10]) \(.visibility)"' 2>/dev/null)" || repos=""
[ -n "$repos" ] || { echo "nudge-hook-coverage: could not list repositories of $ORG" >&2; exit 3; }

rows=(); gaps=(); errors=0; covered=0; skipped=0
while read -r name created pushed vis; do
  [ -n "$name" ] || continue
  hooks="$(gh api "repos/$ORG/$name/hooks" --jq "[.[]|select(.config.url|test(\"$PATTERN\"))]|map(\"\(.id):\(if .active then \"active\" else \"inactive\" end)\")|join(\",\")" 2>/dev/null)"
  rc=$?
  if [ $rc -ne 0 ]; then
    rows+=("$name hooks=? created=$created pushed=$pushed note=hook-list-denied"); errors=$((errors+1)); continue
  fi
  if [ -n "$hooks" ]; then
    rows+=("$name hooks=$hooks created=$created pushed=$pushed"); covered=$((covered+1)); continue
  fi
  # A missing directory is a 404: gh still prints the error body on stdout and
  # exits non-zero, so take the first line only and treat anything that is not
  # a positive integer as "no workflows".
  workflows="$(gh api "repos/$ORG/$name/contents/.github/workflows" --jq 'if type=="array" then length else 0 end' 2>/dev/null | head -1)"
  case "$workflows" in ''|*[!0-9]*) workflows=0 ;; esac
  if [ "$workflows" = "0" ] && [ "$INCLUDE_NO_CI" = 0 ]; then
    rows+=("$name hooks=- created=$created pushed=$pushed note=no-ci-workflows (skipped)"); skipped=$((skipped+1)); continue
  fi
  rows+=("$name hooks=MISSING created=$created pushed=$pushed workflows=${workflows:-0}")
  gaps+=("$name created=$created pushed=$pushed workflows=${workflows:-0}")
done <<< "$repos"

total=$(printf '%s\n' "$repos" | grep -c .)
summary="nudge-hook-coverage: org=$ORG repos=$total covered=$covered missing=${#gaps[@]} skipped_no_ci=$skipped list_denied=$errors"
if [ "$JSON" = 1 ]; then
  printf '%s\n' "${rows[@]}" | jq -R -s -c --arg org "$ORG" --argjson total "$total" --argjson covered "$covered" --argjson missing "${#gaps[@]}" --argjson skipped "$skipped" --argjson denied "$errors" \
    '{org:$org,total:$total,covered:$covered,missing:$missing,skipped_no_ci:$skipped,list_denied:$denied,rows:(split("\n")|map(select(length>0)))}'
else
  printf '%s\n' "${rows[@]}"; echo "$summary"
fi

if [ -n "$COMMENT" ] && { [ "${#gaps[@]}" -gt 0 ] || [ "$ALWAYS" = 1 ]; }; then
  repo="${COMMENT%%#*}"; issue="${COMMENT##*#}"
  body="## nudge hook coverage — $(date -u +%Y-%m-%dT%H:%MZ) (automated, read-only)
$summary
"
  if [ "${#gaps[@]}" -gt 0 ]; then
    body="$body
Repositories with CI workflows but no hook matching \`$PATTERN\`:
$(printf -- '- %s\n' "${gaps[@]}")

Register one hook per repository (same config as the existing relay hook); registration is a separate, approved step."
  else
    body="$body
Full coverage."
  fi
  [ "$errors" -gt 0 ] && body="$body

$errors repositories could not be listed (token lacks admin there)."
  gh issue comment "$issue" --repo "$repo" --body "$body" >/dev/null 2>&1 || echo "nudge-hook-coverage: issue comment failed" >&2
fi

[ "${#gaps[@]}" -eq 0 ] || exit 10
exit 0
