#!/usr/bin/env bash
# Fail-closed owner-repo hygiene through the relay node's existing jinon86 gh session.
#
# Enables Dependabot vulnerability alerts and creates one minimal ruleset
# (block deletion + force-push on the default branch) on the repositories named
# in a node-local allowlist file. It never requires PRs or reviews, so no node
# automation breaks.
#
# The allowlist is node-local config, not canon: repository names are fleet
# data (#1446). One OWNER/REPO per line; blank lines and # comments ignored.
#
# The token never leaves the relay node; this helper sends only repository
# names. Dry-run is the default; --apply mutates. Both need --operator-approved
# because every use of the relay-held credential is a privileged action.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: repo-hygiene-via-relay.sh --operator-approved [--apply] \
  [--repo OWNER/REPO]... [--allowlist FILE] [--ssh-target HOST]

Default is a dry run. The allowlist defaults to $CCC_REPO_HYGIENE_ALLOWLIST or
~/.config/ccc/repo-hygiene-allowlist. --repo narrows it; a name outside the
allowlist is refused. Exit 0 only when every selected repository ends
compliant (or, in a dry run, is admin-reachable with no conflicting ruleset).
EOF
}

ssh_target="${CCC_RELAY_SSH_TARGET:-relay}"
expected_actor="${CCC_RELAY_MERGE_ACTOR:-jinon86}"
allowlist_file="${CCC_REPO_HYGIENE_ALLOWLIST:-${XDG_CONFIG_HOME:-$HOME/.config}/ccc/repo-hygiene-allowlist}"
approved=0
apply=0
selected=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) selected+=("${2:-}"); shift 2 ;;
    --allowlist) allowlist_file="${2:-}"; shift 2 ;;
    --ssh-target) ssh_target="${2:-}"; shift 2 ;;
    --operator-approved) approved=1; shift ;;
    --apply) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ "$approved" -eq 1 ] || {
  echo "refusing: --operator-approved is required for any relay credential use" >&2
  exit 2
}
[[ "$ssh_target" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "invalid --ssh-target" >&2; exit 2; }
[[ "$expected_actor" =~ ^[A-Za-z0-9-]+$ ]] || { echo "invalid expected actor" >&2; exit 2; }

[ -f "$allowlist_file" ] || { echo "refusing: allowlist file not found: $allowlist_file" >&2; exit 2; }
allowlist=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  line="${line//[[:space:]]/}"
  [ -n "$line" ] || continue
  [[ "$line" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
    echo "refusing: invalid allowlist entry (expected OWNER/REPO): $line" >&2
    exit 2
  }
  allowlist+=("$line")
done < "$allowlist_file"
[ "${#allowlist[@]}" -gt 0 ] || { echo "refusing: allowlist is empty: $allowlist_file" >&2; exit 2; }

if [ "${#selected[@]}" -eq 0 ]; then
  selected=("${allowlist[@]}")
else
  for r in "${selected[@]}"; do
    found=0
    for a in "${allowlist[@]}"; do [ "$r" = "$a" ] && { found=1; break; }; done
    [ "$found" -eq 1 ] || { echo "refusing: repository not in allowlist: $r" >&2; exit 2; }
  done
fi
command -v ssh >/dev/null 2>&1 || { echo "ssh is required" >&2; exit 2; }

ssh -o BatchMode=yes -o ConnectTimeout="${CCC_RELAY_SSH_TIMEOUT:-8}" \
  "$ssh_target" bash -s -- \
  "$expected_actor" "$apply" "${selected[@]}" <<'REMOTE'
set -euo pipefail

expected_actor="$1"; apply="$2"; shift 2
ruleset_name="default-branch-no-delete-no-force-push"
payload='{"name":"default-branch-no-delete-no-force-push","target":"branch","enforcement":"active","conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},"rules":[{"type":"deletion"},{"type":"non_fast_forward"}]}'

command -v gh >/dev/null 2>&1 || { echo "remote gh is unavailable" >&2; exit 3; }
command -v jq >/dev/null 2>&1 || { echo "remote jq is unavailable" >&2; exit 3; }

actor="$(gh api user | jq -r .login)"
[ "$actor" = "$expected_actor" ] || {
  echo "refusing: remote actor is not $expected_actor" >&2
  exit 3
}

# A ruleset matches only when it is active on ~DEFAULT_BRANCH with exactly the
# two expected rule types; anything else under our name is a conflict we report
# and never overwrite.
matches='(.enforcement == "active")
  and (.conditions.ref_name.include == ["~DEFAULT_BRANCH"])
  and ([.rules[].type] | sort == ["deletion","non_fast_forward"])'

# 0 = enabled, 1 = disabled (HTTP 404 with admin), 2 = unknown error.
alerts_state() {
  local err
  if err="$(gh api "repos/$1/vulnerability-alerts" 2>&1 >/dev/null)"; then return 0; fi
  if grep -q 'HTTP 404' <<<"$err"; then return 1; fi
  return 2
}

failed=0
for repo in "$@"; do
  row() { jq -cn --arg repo "$repo" "$@"; }
  meta="$(gh api "repos/$repo")" || { row '{repo:$repo,status:"error",reason:"repo-read-failed"}'; failed=1; continue; }
  if [ "$(jq -r '.permissions.admin // false' <<<"$meta")" != "true" ]; then
    row '{repo:$repo,status:"refused",reason:"no-admin"}'; failed=1; continue
  fi
  if [ "$(jq -r '.archived // false' <<<"$meta")" = "true" ]; then
    row '{repo:$repo,status:"refused",reason:"archived"}'; failed=1; continue
  fi

  repo_failed=0

  # 1) Dependabot vulnerability alerts.
  alerts_state "$repo" && rc=0 || rc=$?
  case "$rc" in
    0) da="already-on" ;;
    1)
      if [ "$apply" -eq 1 ]; then
        gh api -X PUT "repos/$repo/vulnerability-alerts" >/dev/null 2>&1 || true
        alerts_state "$repo" && da="enabled" || { da="enable-unverified"; repo_failed=1; }
      else
        da="would-enable"
      fi ;;
    *) da="unknown"; repo_failed=1 ;;
  esac

  # 2) Minimal default-branch ruleset.
  rs=""
  if ! listing="$(gh api "repos/$repo/rulesets")" \
    || ! ids="$(jq -r --arg n "$ruleset_name" '.[] | select(.name == $n) | .id' <<<"$listing")"; then
    rs="unknown"; repo_failed=1
  elif [ -n "$ids" ]; then
    if [ "$(wc -l <<<"$ids")" -ne 1 ]; then
      rs="conflict-duplicate"; repo_failed=1
    elif gh api "repos/$repo/rulesets/$ids" | jq -e "$matches" >/dev/null; then
      rs="already-present(#$ids)"
    else
      rs="conflict-mismatch(#$ids)"; repo_failed=1
    fi
  elif [ "$apply" -eq 1 ]; then
    id="$(gh api -X POST "repos/$repo/rulesets" --input - <<<"$payload" 2>/dev/null | jq -r .id 2>/dev/null)" || id=""
    if [[ "$id" =~ ^[0-9]+$ ]] && gh api "repos/$repo/rulesets/$id" | jq -e "$matches" >/dev/null; then
      rs="created(#$id)"
    else
      rs="create-unverified"; repo_failed=1
    fi
  else
    rs="would-create"
  fi

  if [ "$repo_failed" -eq 0 ]; then status=ok; else status=incomplete; failed=1; fi
  row --arg st "$status" --arg da "$da" --arg rs "$rs" '{repo:$repo,status:$st,dependabot:$da,ruleset:$rs}'
done

jq -cn --arg actor "$actor" --argjson apply "$apply" --argjson failed "$failed" \
  '{summary:true,actor:$actor,apply:($apply == 1),ok:($failed == 0)}'
[ "$failed" -eq 0 ] || exit 4
REMOTE
