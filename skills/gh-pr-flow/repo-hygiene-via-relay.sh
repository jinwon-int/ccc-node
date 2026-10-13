#!/usr/bin/env bash
# Fail-closed owner-repo hygiene through the relay node's existing jinon86 gh session.
#
# Enables Dependabot vulnerability alerts and creates one minimal ruleset
# (block deletion + force-push on the default branch) on the repositories named
# in a node-local allowlist file. That default-branch ruleset never requires PRs
# or reviews, so no node automation breaks.
#
# Opt-in per repository (#2292): `protect-branch=NAME` tokens after the repo
# name also ensure a `protected-branch-NAME` ruleset on refs/heads/NAME —
# deletion + force-push blocked AND a pull request with one approval from
# someone other than the last pusher (stale approvals dismissed, merge commits
# only, no bypass actors). Meant for promotion branches that nodes deploy from
# (e.g. a `stable` ref a scheduled self-update follows), so moving them needs a
# reviewed PR. The default branch, master and main are refused: requiring PRs
# there would break node automation that pushes or auto-merges.
#
# The allowlist is node-local config, not canon: repository and branch names
# are fleet data (#1446). One `OWNER/REPO [protect-branch=NAME]...` per line;
# blank lines and # comments ignored.
#
# The token never leaves the relay node; this helper sends only repository
# names. Dry-run is the default; --apply mutates. Both need --operator-approved
# because every use of the relay-held credential is a privileged action.
set -euo pipefail
# Name checks below are bracket regexes; under a UTF-8 locale [A-Za-z] can
# match non-ASCII letters (#2292 review), so pin the C locale.
export LC_ALL=C
# declare -A and ${var,,} below; refuse rather than misparse on bash 3.
[ "${BASH_VERSINFO[0]:-0}" -ge 4 ] || { echo "refusing: bash >= 4 is required" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: repo-hygiene-via-relay.sh --operator-approved [--apply] \
  [--repo OWNER/REPO]... [--allowlist FILE] [--ssh-target HOST]

Default is a dry run. The allowlist defaults to $CCC_REPO_HYGIENE_ALLOWLIST or
~/.config/ccc/repo-hygiene-allowlist; each line is
`OWNER/REPO [protect-branch=NAME]...`. --repo narrows it; a name outside the
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
declare -A protect_branches=()
while IFS= read -r line || [ -n "$line" ]; do
  read -r -a raw_fields <<<"$line"
  # '#' starts a comment only at a field boundary (or inside the repo field,
  # which cannot contain it); a '#' inside a branch token is kept and then
  # refused below instead of silently truncating the name (#2292 review).
  fields=()
  for f in "${raw_fields[@]+"${raw_fields[@]}"}"; do
    [[ "$f" == '#'* ]] && break
    if [ "${#fields[@]}" -eq 0 ] && [[ "$f" == *'#'* ]]; then fields+=("${f%%#*}"); break; fi
    fields+=("$f")
  done
  [ "${#fields[@]}" -gt 0 ] && [ -n "${fields[0]}" ] || continue
  repo_entry="${fields[0]}"
  [[ "$repo_entry" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
    echo "refusing: invalid allowlist entry (expected OWNER/REPO): ${line//[[:space:]]/ }" >&2
    exit 2
  }
  branches="${protect_branches[$repo_entry]:-}"
  for tok in "${fields[@]:1}"; do
    case "$tok" in
      protect-branch=*) b="${tok#protect-branch=}" ;;
      *) echo "refusing: invalid allowlist token for $repo_entry (expected protect-branch=NAME): $tok" >&2; exit 2 ;;
    esac
    # A plain branch name: no refs/ prefix, no '..', no leading '-' or '/'.
    if ! [[ "$b" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$ ]] \
      || [[ "$b" == *..* || "$b" == refs/* || "$b" == *.lock ]]; then
      echo "refusing: invalid protect-branch name for $repo_entry: $b" >&2; exit 2
    fi
    case "${b,,}" in
      master|main|head)
        echo "refusing: protect-branch=$b for $repo_entry — PR-only rules on master/main would break node automation" >&2; exit 2 ;;
    esac
    case ",$branches," in *",$b,"*) ;; *) branches="${branches:+$branches,}$b" ;; esac
  done
  case " ${allowlist[*]-} " in *" $repo_entry "*) ;; *) allowlist+=("$repo_entry") ;; esac
  protect_branches[$repo_entry]="$branches"
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

# Remote arguments are `OWNER/REPO:b1,b2` (nothing after ':' = no extra
# branch). ssh joins its arguments into ONE string that the relay's login shell
# re-parses, so the separators must be shell-inert: ':' and ',' are, and
# neither can occur in a validated repo or branch name. ('|' was a pipe there —
# #2292 review.) Every name was validated above against a strict ASCII set.
remote_args=()
for r in "${selected[@]}"; do remote_args+=("$r:${protect_branches[$r]:-}"); done

ssh -o BatchMode=yes -o ConnectTimeout="${CCC_RELAY_SSH_TIMEOUT:-8}" \
  "$ssh_target" bash -s -- \
  "$expected_actor" "$apply" "${remote_args[@]}" <<'REMOTE'
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
matches='(.enforcement == "active") and (.target == "branch")
  and (.conditions.ref_name.include == ["~DEFAULT_BRANCH"])
  and ((.conditions.ref_name.exclude // []) == [])
  and ([.rules[].type] | sort == ["deletion","non_fast_forward"])'

# 0 = enabled, 1 = disabled (HTTP 404 with admin), 2 = unknown error.
alerts_state() {
  local err
  if err="$(gh api "repos/$1/vulnerability-alerts" 2>&1 >/dev/null)"; then return 0; fi
  if grep -q 'HTTP 404' <<<"$err"; then return 1; fi
  return 2
}

# protected-branch-<name> (#2292): the payload and the acceptance test for an
# existing ruleset of that name. Matching is strict on what makes the branch
# safe (target, no bypass, PR with an independent approval) and tolerant of
# GitHub filling in defaults for parameters we do not set.
branch_payload() {
  jq -cn --arg n "protected-branch-$1" --arg ref "refs/heads/$1" '{
    name:$n, target:"branch", enforcement:"active", bypass_actors:[],
    conditions:{ref_name:{include:[$ref],exclude:[]}},
    rules:[{type:"deletion"},{type:"non_fast_forward"},
      {type:"pull_request",parameters:{
        required_approving_review_count:1, dismiss_stale_reviews_on_push:true,
        require_last_push_approval:true, require_code_owner_review:false,
        required_review_thread_resolution:false, allowed_merge_methods:["merge"]}}]}'
}
branch_match() {
  # Every property the SKILL.md promises is checked here: an exclude pattern,
  # a tag target, a missing/non-empty bypass list, a non-"never" bypass for the
  # caller, or other merge methods each make the ruleset weaker (#2292 review).
  printf '%s' '(.enforcement == "active") and (.target == "branch")
    and (.conditions.ref_name.include == ['"$(jq -cn --arg r "refs/heads/$1" '$r')"'])
    and ((.conditions.ref_name.exclude // []) == [])
    and ((.bypass_actors | type) == "array") and ((.bypass_actors | length) == 0)
    and ((.current_user_can_bypass // "never") == "never")
    and ([.rules[].type] | sort == ["deletion","non_fast_forward","pull_request"])
    and ([.rules[] | select(.type == "pull_request") | .parameters
          | ((.required_approving_review_count // 0) >= 1)
            and (.require_last_push_approval == true)
            and (.dismiss_stale_reviews_on_push == true)
            and (.allowed_merge_methods == ["merge"])] | all)'
}

# Ensure one named ruleset. Sets ens_status; returns 1 unless compliant (or, in
# a dry run, creatable). Same-name rulesets that differ are reported and never
# overwritten; a created one is re-read and must match before it counts.
ensure_ruleset() {
  local repo="$1" name="$2" body="$3" test="$4" listing ids id
  if ! listing="$(gh api "repos/$repo/rulesets")" \
    || ! ids="$(jq -r --arg n "$name" '.[] | select(.name == $n) | .id' <<<"$listing")"; then
    ens_status="unknown"; return 1
  fi
  if [ -n "$ids" ]; then
    if [ "$(wc -l <<<"$ids")" -ne 1 ]; then
      ens_status="conflict-duplicate"; return 1
    elif gh api "repos/$repo/rulesets/$ids" | jq -e "$test" >/dev/null; then
      ens_status="already-present(#$ids)"; return 0
    fi
    ens_status="conflict-mismatch(#$ids)"; return 1
  fi
  if [ "$apply" -ne 1 ]; then
    ens_status="would-create"; return 0
  fi
  id="$(gh api -X POST "repos/$repo/rulesets" --input - <<<"$body" 2>/dev/null | jq -r .id 2>/dev/null)" || id=""
  if [[ "$id" =~ ^[0-9]+$ ]] && gh api "repos/$repo/rulesets/$id" | jq -e "$test" >/dev/null; then
    ens_status="created(#$id)"; return 0
  fi
  ens_status="create-unverified"; return 1
}

failed=0
for entry in "$@"; do
  repo="${entry%%:*}"
  extra_branches=""
  case "$entry" in *:*) extra_branches="${entry#*:}" ;; esac
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
  ensure_ruleset "$repo" "$ruleset_name" "$payload" "$matches" || repo_failed=1
  rs="$ens_status"

  # 3) Opt-in PR-only rulesets on promotion branches (#2292).
  default_branch="$(jq -r '.default_branch // ""' <<<"$meta")"
  pb=()
  if [ -n "$extra_branches" ]; then
    IFS=, read -r -a extra <<<"$extra_branches"
    for b in "${extra[@]}"; do
      if [ -z "$default_branch" ] || [ "$b" = "$default_branch" ]; then
        # Unknown default branch fails closed: never risk PR-gating it.
        pb+=("$b:refused-default-branch"); repo_failed=1; continue
      fi
      ensure_ruleset "$repo" "protected-branch-$b" "$(branch_payload "$b")" "$(branch_match "$b")" || repo_failed=1
      pb+=("$b:$ens_status")
    done
  fi
  pb_json="$(printf '%s\n' "${pb[@]+"${pb[@]}"}" | jq -R 'select(length > 0)' | jq -cs .)"

  if [ "$repo_failed" -eq 0 ]; then status=ok; else status=incomplete; failed=1; fi
  row --arg st "$status" --arg da "$da" --arg rs "$rs" --argjson pb "$pb_json" \
    '{repo:$repo,status:$st,dependabot:$da,ruleset:$rs,protected_branches:$pb}'
done

jq -cn --arg actor "$actor" --argjson apply "$apply" --argjson failed "$failed" \
  '{summary:true,actor:$actor,apply:($apply == 1),ok:($failed == 0)}'
[ "$failed" -eq 0 ] || exit 4
REMOTE
