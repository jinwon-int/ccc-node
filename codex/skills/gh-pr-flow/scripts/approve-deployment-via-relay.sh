#!/usr/bin/env bash
# Approve one pending GitHub Actions environment deployment (a required-reviewer
# gate such as a `release` environment) through the relay-held jinon86 profile.
# Credentials remain on the relay node and are never printed, copied, or
# reconfigured. This is a release-gate action: every invocation needs fresh
# explicit user approval for the exact repository, run, environment, and head.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: approve-deployment-via-relay.sh --repo jinwon-int/REPO --run-id N \
  --environment NAME --expected-head 40_HEX_SHA \
  [--expected-workflow .github/workflows/release.yml] [--expected-branch main] \
  [--expected-event workflow_dispatch] [--ssh-target HOST] [--dry-run] \
  --operator-approved

Requires CCC_EXPLICIT_USER_APPROVAL=1. Exit 0 means the environment is approved
for this run by jinon86 (now, or already by an earlier invocation). Exit 66
means the run is no longer waiting and jinon86 never approved the environment;
the state is reported on stdout and nothing is submitted.
EOF
}

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit "${2:-64}"
}

repo=""
run_id=""
environment=""
expected_head=""
expected_workflow=".github/workflows/release.yml"
expected_branch="main"
expected_event="workflow_dispatch"
ssh_target="${CCC_RELAY_SSH_TARGET:-relay}"
approved=0
dry_run=0
# The approval profile is fixed: jinon86 is the environments' required reviewer.
expected_actor="jinon86"
review_config="${CCC_JINON86_GH_CONFIG_DIR:-/root/.config/gh}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) repo="${2:-}"; shift 2 ;;
    --run-id) run_id="${2:-}"; shift 2 ;;
    --environment) environment="${2:-}"; shift 2 ;;
    --expected-head) expected_head="${2:-}"; shift 2 ;;
    --expected-workflow) expected_workflow="${2:-}"; shift 2 ;;
    --expected-branch) expected_branch="${2:-}"; shift 2 ;;
    --expected-event) expected_event="${2:-}"; shift 2 ;;
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
  || die "--operator-approved is required for this exact run and environment"
[[ "$repo" =~ ^jinwon-int/[A-Za-z0-9_.-]+$ ]] \
  || die "repository must match jinwon-int/REPO"
[[ "$run_id" =~ ^[1-9][0-9]{0,19}$ ]] \
  || die "run id must be a positive integer"
[[ "$environment" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] \
  || die "environment must match [A-Za-z0-9_.-]{1,64}"
[[ "$expected_head" =~ ^[0-9a-fA-F]{40}$ ]] \
  || die "expected head must be a full 40-character SHA"
[[ "$expected_workflow" =~ ^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$ ]] \
  || die "expected workflow must be .github/workflows/NAME.yml"
[[ "$expected_branch" =~ ^[A-Za-z0-9._/-]{1,255}$ ]] \
  && [[ "/$expected_branch/" != *"/../"* ]] \
  || die "invalid expected branch"
[[ "$expected_event" =~ ^[a-z_]{1,64}$ ]] \
  || die "invalid expected event"
[[ "$ssh_target" =~ ^[A-Za-z0-9_.@-]+$ ]] \
  || die "invalid SSH target"
[[ "$review_config" =~ ^/[A-Za-z0-9_./-]+$ ]] \
  || die "invalid remote gh config directory"
[[ "/$review_config/" != *"/../"* ]] \
  || die "remote gh config directory must not contain parent traversal"

# The source node goes into the recorded approval comment only. An unusable
# value is replaced rather than trusted.
source_node="${CCC_NODE:-$(hostname -s 2>/dev/null || true)}"
[[ "$source_node" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || source_node="unknown"
command -v ssh >/dev/null 2>&1 || die "ssh is unavailable" 69

ssh -o BatchMode=yes -o ConnectTimeout=8 "$ssh_target" bash -s -- \
  "$repo" "$run_id" "$environment" "${expected_head,,}" "$expected_workflow" \
  "$expected_branch" "$expected_event" "$review_config" "$dry_run" \
  "$expected_actor" "$source_node" <<'REMOTE'
set -euo pipefail

repo="$1"
run_id="$2"
environment="$3"
expected_head="$4"
expected_workflow="$5"
expected_branch="$6"
expected_event="$7"
review_config="$8"
dry_run="$9"
expected_actor="${10}"
source_node="${11}"
credential_file="$review_config/hosts.yml"

fail() {
  echo "ERROR: $1" >&2
  exit "${2:-65}"
}

command -v gh >/dev/null 2>&1 || fail "remote gh is unavailable" 69
command -v jq >/dev/null 2>&1 || fail "remote jq is unavailable" 69
[ -d "$review_config" ] && [ ! -L "$review_config" ] \
  || fail "isolated gh config directory is unsafe"
[ -f "$credential_file" ] && [ ! -L "$credential_file" ] \
  || fail "isolated gh credential file is unsafe"
dir_mode="$(stat -c '%a' "$review_config")"
[ "$(stat -c '%U:%G' "$review_config")" = "root:root" ] \
  && (( (8#$dir_mode & 8#022) == 0 )) \
  || fail "isolated gh config directory owner or write mode is unsafe"
[ "$(stat -c '%a:%U:%G' "$credential_file")" = "600:root:root" ] \
  || fail "isolated gh credential file owner or mode is unsafe"

review_gh() {
  GH_CONFIG_DIR="$review_config" gh "$@"
}

actor="$(review_gh api user --jq .login)"
[ "$actor" = "$expected_actor" ] || fail "expected approval actor $expected_actor"

# 1. The run must be exactly the one the operator approved.
run_json="$(review_gh api "repos/$repo/actions/runs/$run_id")"
repo_lc="${repo,,}"
[ "$(jq -r '.repository.full_name // "" | ascii_downcase' <<<"$run_json")" = "$repo_lc" ] \
  || fail "run does not belong to the repository"
[ "$(jq -r '.head_repository.full_name // "" | ascii_downcase' <<<"$run_json")" = "$repo_lc" ] \
  || fail "run head repository is not the repository itself"
[ "$(jq -r '.head_sha // "" | ascii_downcase' <<<"$run_json")" = "$expected_head" ] \
  || fail "run head does not match --expected-head"
[ "$(jq -r '.head_branch // ""' <<<"$run_json")" = "$expected_branch" ] \
  || fail "run branch does not match --expected-branch"
[ "$(jq -r '.event // ""' <<<"$run_json")" = "$expected_event" ] \
  || fail "run event does not match --expected-event"
[ "$(jq -r '.path // ""' <<<"$run_json")" = "$expected_workflow" ] \
  || fail "run workflow does not match --expected-workflow"
run_status="$(jq -r '.status // ""' <<<"$run_json")"
run_conclusion="$(jq -r '.conclusion // ""' <<<"$run_json")"
run_attempt="$(jq -r '.run_attempt // 0' <<<"$run_json")"
[[ "$run_attempt" =~ ^[0-9]+$ ]] || fail "run attempt is invalid"

# Approvals this actor has recorded for this environment on this run. The
# review history carries no attempt number, so a re-run attempt that gates the
# same environment again is detected from the pending list below, not from here.
approval_count() {
  local history
  history="$(review_gh api "repos/$repo/actions/runs/$run_id/approvals")"
  jq --arg actor "$actor" --arg env "$environment" \
    '[.[]? | select(.state == "approved" and .user.login == $actor and
      any(.environments[]?; .name == $env))] | length' <<<"$history"
}

report() { # <ok> <approved> <already_approved> <environment_id|""> <note>
  jq -n --argjson ok "$1" --argjson approved "$2" --argjson already "$3" \
    --arg env_id "$4" --arg note "$5" \
    --argjson dry_run "$dry_run" --arg repo "$repo" --arg run_id "$run_id" \
    --argjson attempt "$run_attempt" --arg env "$environment" \
    --arg actor "$actor" --arg head "$expected_head" \
    --arg branch "$expected_branch" --arg event "$expected_event" \
    --arg workflow "$expected_workflow" --arg status "$run_status" \
    --arg conclusion "$run_conclusion" --arg node "$source_node" \
    '{ok:$ok,approved:$approved,already_approved:$already,
      dry_run:($dry_run == 1),note:$note,repo:$repo,
      run_id:($run_id|tonumber),run_attempt:$attempt,environment:$env,
      environment_id:(if $env_id == "" then null else ($env_id|tonumber) end),
      actor:$actor,head:$head,branch:$branch,event:$event,workflow:$workflow,
      run_status:$status,
      run_conclusion:(if $conclusion == "" then null else $conclusion end),
      source_node:$node}'
}

before_count="$(approval_count)"
[[ "$before_count" =~ ^[0-9]+$ ]] || fail "could not read the run's approval history"

# 2. Idempotence: a run that is no longer waiting is reported, never approved.
if [ "$run_status" != "waiting" ]; then
  if [ "$before_count" -gt 0 ]; then
    report true true true "" "run is no longer waiting; $actor already approved this environment"
    exit 0
  fi
  report false false false "" "run is not waiting and $actor has not approved this environment; nothing submitted"
  exit 66
fi

# 3. Exactly one pending environment, the requested one, approvable by actor.
pending_json="$(review_gh api "repos/$repo/actions/runs/$run_id/pending_deployments")"
jq -e 'type == "array"' >/dev/null <<<"$pending_json" \
  || fail "pending deployments response is not a list"
env_pending="$(jq --arg env "$environment" \
  '[.[] | select(.environment.name == $env)] | length' <<<"$pending_json")"
if [ "$env_pending" -eq 0 ]; then
  if [ "$before_count" -gt 0 ]; then
    report true true true "" "environment is no longer pending; $actor already approved it"
    exit 0
  fi
  fail "environment $environment is not pending on this run"
fi
[ "$(jq 'length' <<<"$pending_json")" -eq 1 ] \
  || fail "more than one environment is pending; refusing an ambiguous approval"
env_id="$(jq -r '.[0].environment.id // "" | tostring' <<<"$pending_json")"
[[ "$env_id" =~ ^[1-9][0-9]{0,19}$ ]] || fail "pending environment id is invalid"
[ "$(jq -r '.[0].current_user_can_approve' <<<"$pending_json")" = "true" ] \
  || fail "$actor cannot approve environment $environment on this run"

if [ "$dry_run" -eq 1 ]; then
  report true false false "$env_id" "dry run: all gates passed; nothing submitted"
  exit 0
fi

comment="Environment $environment approved for run $run_id (attempt $run_attempt) at exact head $expected_head after fresh explicit operator approval, via the relay-held $actor credential (ccc-node approve-deployment-via-relay.sh) requested from node $source_node."
review_gh api --method POST \
  "repos/$repo/actions/runs/$run_id/pending_deployments" \
  -F "environment_ids[]=$env_id" \
  -f state=approved \
  -f "comment=$comment" >/dev/null

# 4. Verify from GitHub's own records, not from the POST response.
after_count="$(approval_count)"
[[ "$after_count" =~ ^[0-9]+$ ]] && [ "$after_count" -gt "$before_count" ] \
  || fail "the environment approval was not recorded"
after_pending="$(review_gh api "repos/$repo/actions/runs/$run_id/pending_deployments")"
[ "$(jq --arg env "$environment" \
  '[.[]? | select(.environment.name == $env)] | length' <<<"$after_pending")" -eq 0 ] \
  || fail "environment $environment is still pending after approval"
run_status="$(review_gh api "repos/$repo/actions/runs/$run_id" --jq '.status // ""')"

report true true false "$env_id" "environment approved"
REMOTE
