#!/usr/bin/env bash
# Hermetic tests for approve-deployment-via-relay.sh: ssh, gh, and stat are
# stubbed; the stubbed gh keeps run/pending/approval state in fixture files so
# the helper's post-approval verification reads what its POST changed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/codex/skills/gh-pr-flow/scripts/approve-deployment-via-relay.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); }
bad() { printf 'FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }

mkdir -p "$TMP/bin" "$TMP/review-config" "$TMP/state"
printf 'test fixture; not a credential\n' >"$TMP/review-config/hosts.yml"
chmod 700 "$TMP/review-config"
chmod 600 "$TMP/review-config/hosts.yml"

cat >"$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
while [ "${1:-}" = "-o" ]; do shift 2; done
[ "${1:-}" = "relay-test" ] || { echo "unexpected SSH target" >&2; exit 90; }
shift
: >"$MOCK_SSH_MARKER"
exec "$@"
EOF

# State files: run.json, pending.json, approvals.json under $MOCK_STATE.
cat >"$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${GH_CONFIG_DIR:-}" = "$MOCK_EXPECTED_CONFIG" ] || {
  echo "approval gh did not use the allowlisted config directory" >&2
  exit 91
}
printf '%s\n' "$*" >>"$MOCK_GH_LOG"
[ "${1:-}" = "api" ] || { printf 'unexpected gh call: %s\n' "$*" >&2; exit 92; }
shift
emit() { # <json-file> [--jq FILTER]
  if [ "${2:-}" = "--jq" ]; then jq -r "$3" "$1"; else cat "$1"; fi
}
case "$1" in
  user)
    printf '%s\n' "${MOCK_ACTOR:-jinon86}"
    ;;
  --method)
    [ "$2 $3" = "POST repos/$MOCK_REPO/actions/runs/$MOCK_RUN/pending_deployments" ] \
      || { printf 'unexpected POST: %s\n' "$*" >&2; exit 92; }
    printf '%s\n' "$*" >>"$MOCK_POST_MARKER"
    if [ "${MOCK_POST_EFFECT:-record}" = "record" ]; then
      jq --arg env "$MOCK_ENV" \
        '. + [{state:"approved",user:{login:"jinon86"},comment:"x",
               environments:[{id:4242,name:$env}]}]' \
        "$MOCK_STATE/approvals.json" >"$MOCK_STATE/approvals.tmp"
      mv "$MOCK_STATE/approvals.tmp" "$MOCK_STATE/approvals.json"
      printf '[]\n' >"$MOCK_STATE/pending.json"
    fi
    printf '[]\n'
    ;;
  "repos/$MOCK_REPO/actions/runs/$MOCK_RUN")
    emit "$MOCK_STATE/run.json" "${2:-}" "${3:-}"
    ;;
  "repos/$MOCK_REPO/actions/runs/$MOCK_RUN/pending_deployments")
    emit "$MOCK_STATE/pending.json"
    ;;
  "repos/$MOCK_REPO/actions/runs/$MOCK_RUN/approvals")
    emit "$MOCK_STATE/approvals.json"
    ;;
  *)
    printf 'unexpected gh call: api %s\n' "$*" >&2
    exit 92
    ;;
esac
EOF

cat >"$TMP/bin/stat" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "-c" ] || { echo "unexpected stat invocation" >&2; exit 93; }
case "${2:-}:${3:-}" in
  "%a:$MOCK_EXPECTED_CONFIG") printf '%s\n' '700' ;;
  "%U:%G:$MOCK_EXPECTED_CONFIG") printf '%s\n' 'root:root' ;;
  "%a:%U:%G:$MOCK_EXPECTED_CONFIG/hosts.yml")
    printf '%s\n' "${MOCK_CREDENTIAL_STAT:-600:root:root}" ;;
  *) echo "unexpected stat target" >&2; exit 93 ;;
esac
EOF
chmod +x "$TMP/bin/ssh" "$TMP/bin/gh" "$TMP/bin/stat"

export PATH="$TMP/bin:$PATH"
export MOCK_SSH_MARKER="$TMP/ssh.called"
export MOCK_POST_MARKER="$TMP/post.called"
export MOCK_GH_LOG="$TMP/gh.calls"
export MOCK_EXPECTED_CONFIG="$TMP/review-config"
export MOCK_STATE="$TMP/state"
export MOCK_REPO="jinwon-int/example"
export MOCK_RUN="36504921670"
export MOCK_ENV="release"
HEAD_SHA=ce61ddbe86827e468535ae6964b4fc5202c20362
OTHER_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

# reset_state [key=value ...] — a waiting release run gated on one environment
# that jinon86 can approve, with no prior approvals. Overrides are jq paths.
reset_state() {
  rm -f "$MOCK_SSH_MARKER" "$MOCK_POST_MARKER"
  jq -n --arg repo "$MOCK_REPO" --arg head "$HEAD_SHA" \
    '{status:"waiting",conclusion:null,run_attempt:1,head_sha:$head,
      head_branch:"main",event:"workflow_dispatch",
      path:".github/workflows/release.yml",
      repository:{full_name:$repo},head_repository:{full_name:$repo}}' \
    >"$MOCK_STATE/run.json"
  jq -n --arg env "$MOCK_ENV" \
    '[{environment:{id:4242,name:$env},current_user_can_approve:true,
       wait_timer:0,reviewers:[]}]' >"$MOCK_STATE/pending.json"
  printf '[]\n' >"$MOCK_STATE/approvals.json"
}
set_run() { # <jq-assignment>
  jq "$1" "$MOCK_STATE/run.json" >"$MOCK_STATE/run.tmp"
  mv "$MOCK_STATE/run.tmp" "$MOCK_STATE/run.json"
}
set_json() { # <file> <json>
  printf '%s\n' "$2" >"$MOCK_STATE/$1"
}
prior_approval() {
  set_json approvals.json "[{\"state\":\"approved\",\"user\":{\"login\":\"jinon86\"},\"comment\":\"x\",\"environments\":[{\"id\":4242,\"name\":\"$MOCK_ENV\"}]}]"
}
post_count() {
  if [ -e "$MOCK_POST_MARKER" ]; then wc -l <"$MOCK_POST_MARKER"; else echo 0; fi
}

run_helper() {
  CCC_EXPLICIT_USER_APPROVAL=1 CCC_NODE=testnode \
  CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
    bash "$HELPER" --repo "$MOCK_REPO" --run-id "$MOCK_RUN" \
      --environment "$MOCK_ENV" --expected-head "$HEAD_SHA" \
      --ssh-target relay-test --operator-approved "$@"
}

# refuses_before_ssh <label> <args...> — local validation must stop the call
# before the relay node is ever contacted.
refuses_before_ssh() {
  local label="$1"; shift
  reset_state
  if "$@" >"$TMP/local.out" 2>&1; then
    bad "$label: helper accepted the call"
  elif [ -e "$MOCK_SSH_MARKER" ]; then
    bad "$label: helper contacted the relay before refusing"
  else
    ok
  fi
}

# refuses_remote <label> <expected-error> [helper args...] — a remote gate must
# fail closed with its own message and submit nothing.
refuses_remote() {
  local label="$1" message="$2"; shift 2
  if run_helper "$@" >"$TMP/remote.out" 2>&1; then
    bad "$label: helper accepted the call"
  elif [ "$(post_count)" -ne 0 ]; then
    bad "$label: helper submitted an approval before refusing"
  elif ! grep -Fq "$message" "$TMP/remote.out"; then
    bad "$label: helper failed for the wrong reason: $(tail -1 "$TMP/remote.out")"
  else
    ok
  fi
}

# --- local gates -----------------------------------------------------------
refuses_before_ssh "missing fresh explicit approval" \
  env CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" bash "$HELPER" \
    --repo "$MOCK_REPO" --run-id "$MOCK_RUN" --environment release \
    --expected-head "$HEAD_SHA" --ssh-target relay-test --operator-approved
refuses_before_ssh "missing --operator-approved" \
  env CCC_EXPLICIT_USER_APPROVAL=1 bash "$HELPER" \
    --repo "$MOCK_REPO" --run-id "$MOCK_RUN" --environment release \
    --expected-head "$HEAD_SHA" --ssh-target relay-test
refuses_before_ssh "repository outside jinwon-int" \
  run_helper --repo other-owner/example
refuses_before_ssh "non-numeric run id" run_helper --run-id 12ab
refuses_before_ssh "zero run id" run_helper --run-id 0
refuses_before_ssh "environment with shell metacharacters" \
  run_helper --environment 'release;id'
refuses_before_ssh "environment with a space" run_helper --environment 'rel ease'
refuses_before_ssh "short expected head" run_helper --expected-head abc123
refuses_before_ssh "workflow outside .github/workflows" \
  run_helper --expected-workflow ../release.yml
refuses_before_ssh "branch with parent traversal" \
  run_helper --expected-branch 'a/../main'
refuses_before_ssh "invalid event" run_helper --expected-event 'push;x'
refuses_before_ssh "invalid ssh target" run_helper --ssh-target 'relay test'
refuses_before_ssh "unknown argument" run_helper --review-profile seoseo-ai
refuses_before_ssh "relative gh config directory" \
  env CCC_EXPLICIT_USER_APPROVAL=1 CCC_JINON86_GH_CONFIG_DIR=relative/gh \
  bash "$HELPER" --repo "$MOCK_REPO" --run-id "$MOCK_RUN" \
    --environment release --expected-head "$HEAD_SHA" \
    --ssh-target relay-test --operator-approved

# --- dry run ---------------------------------------------------------------
reset_state
if run_helper --dry-run >"$TMP/dry.out" \
   && jq -e '.ok == true and .dry_run == true and .approved == false
             and .environment_id == 4242 and .actor == "jinon86"' \
     "$TMP/dry.out" >/dev/null \
   && [ "$(post_count)" -eq 0 ]; then
  ok
else
  bad "valid dry run failed or submitted an approval"
fi

# --- approval --------------------------------------------------------------
reset_state
if run_helper >"$TMP/approve.out" \
   && jq -e '.ok == true and .approved == true and .already_approved == false
             and .environment_id == 4242 and .head == "'"$HEAD_SHA"'"
             and .source_node == "testnode"' "$TMP/approve.out" >/dev/null \
   && [ "$(post_count)" -eq 1 ] \
   && grep -Fq 'environment_ids[]=4242' "$MOCK_POST_MARKER" \
   && grep -Fq 'state=approved' "$MOCK_POST_MARKER" \
   && grep -Fq 'fresh explicit operator approval' "$MOCK_POST_MARKER" \
   && grep -Fq 'from node testnode' "$MOCK_POST_MARKER" \
   && grep -Fq "exact head $HEAD_SHA" "$MOCK_POST_MARKER"; then
  ok
else
  bad "exact-run environment approval failed or posted the wrong payload"
fi

# Idempotence: a re-run right after success finds the environment no longer
# pending and jinon86's approval recorded, and posts nothing.
rm -f "$MOCK_POST_MARKER"
if run_helper >"$TMP/rerun.out" \
   && jq -e '.ok == true and .approved == true and .already_approved == true' \
     "$TMP/rerun.out" >/dev/null \
   && [ "$(post_count)" -eq 0 ]; then
  ok
else
  bad "re-run re-approved an environment jinon86 had already approved"
fi

# Idempotence: the run has moved on (completed) after jinon86 approved it.
reset_state
set_run '.status = "completed" | .conclusion = "success"'
set_json pending.json '[]'
prior_approval
if run_helper >"$TMP/completed-approved.out" \
   && jq -e '.ok == true and .already_approved == true and .run_status == "completed"' \
     "$TMP/completed-approved.out" >/dev/null \
   && [ "$(post_count)" -eq 0 ]; then
  ok
else
  bad "completed run approved by jinon86 was not reported as already approved"
fi

# A run that is no longer waiting and was never approved by jinon86 is
# reported with exit 66, nothing submitted.
reset_state
set_run '.status = "completed" | .conclusion = "cancelled"'
set_json pending.json '[]'
rc=0
run_helper >"$TMP/completed-unapproved.out" 2>&1 || rc=$?
if [ "$rc" -eq 66 ] \
   && jq -e '.ok == false and .approved == false and .run_conclusion == "cancelled"' \
     "$TMP/completed-unapproved.out" >/dev/null \
   && [ "$(post_count)" -eq 0 ]; then
  ok
else
  bad "non-waiting unapproved run was not reported with exit 66 (rc=$rc)"
fi

# A re-run attempt gates the same environment again: the earlier approval in
# the run history must not suppress the new, pending one.
reset_state
set_run '.run_attempt = 2'
prior_approval
if run_helper >"$TMP/reattempt.out" \
   && jq -e '.approved == true and .already_approved == false and .run_attempt == 2' \
     "$TMP/reattempt.out" >/dev/null \
   && [ "$(post_count)" -eq 1 ]; then
  ok
else
  bad "a re-run attempt pending again was skipped because of an older approval"
fi

# Upper-case expected heads are normalized, like the PR approval helper.
reset_state
if CCC_EXPLICIT_USER_APPROVAL=1 CCC_NODE=testnode \
   CCC_JINON86_GH_CONFIG_DIR="$TMP/review-config" \
   bash "$HELPER" --repo "$MOCK_REPO" --run-id "$MOCK_RUN" --environment release \
     --expected-head "${HEAD_SHA^^}" --ssh-target relay-test \
     --operator-approved --dry-run >"$TMP/upper.out" \
   && jq -e '.ok == true' "$TMP/upper.out" >/dev/null; then
  ok
else
  bad "upper-case expected head was not normalized"
fi

# --- remote fail-closed gates ----------------------------------------------
reset_state
refuses_remote "run head changed" "run head does not match" --expected-head "$OTHER_SHA"
reset_state; set_run '.head_branch = "feature"'
refuses_remote "run on another branch" "run branch does not match"
reset_state; set_run '.event = "push"'
refuses_remote "run from another event" "run event does not match"
reset_state; set_run '.path = ".github/workflows/ci.yml"'
refuses_remote "run of another workflow" "run workflow does not match"
reset_state; set_run '.repository.full_name = "jinwon-int/other"'
refuses_remote "run of another repository" "run does not belong to the repository"
reset_state; set_run '.head_repository.full_name = "fork-owner/example"'
refuses_remote "run from a fork" "run head repository is not the repository itself"
reset_state
MOCK_ACTOR=seoseo-ai refuses_remote "wrong remote actor" "expected approval actor jinon86"
reset_state
MOCK_CREDENTIAL_STAT=644:root:root \
  refuses_remote "credential file readable by others" "credential file owner or mode is unsafe"
reset_state
set_json pending.json '[{"environment":{"id":4242,"name":"release"},"current_user_can_approve":true},{"environment":{"id":5151,"name":"prod"},"current_user_can_approve":true}]'
refuses_remote "two environments pending" "more than one environment is pending"
reset_state
set_json pending.json '[{"environment":{"id":5151,"name":"prod"},"current_user_can_approve":true}]'
refuses_remote "requested environment not pending" "environment release is not pending"
reset_state
set_json pending.json '[{"environment":{"id":4242,"name":"release"},"current_user_can_approve":false}]'
refuses_remote "actor cannot approve" "jinon86 cannot approve environment release"
reset_state
set_json pending.json '[{"environment":{"id":"42x","name":"release"},"current_user_can_approve":true}]'
refuses_remote "invalid environment id" "pending environment id is invalid"

# The POST went out but GitHub never recorded it: fail, do not report success.
reset_state
if MOCK_POST_EFFECT=none run_helper >"$TMP/unrecorded.out" 2>&1; then
  bad "helper reported success without a recorded environment approval"
elif grep -Fq "environment approval was not recorded" "$TMP/unrecorded.out" \
   && [ "$(post_count)" -eq 1 ]; then
  ok
else
  bad "unrecorded approval failed for the wrong reason"
fi

# --- credential hygiene ----------------------------------------------------
if grep -Eq 'auth (token|status|login|switch)' "$MOCK_GH_LOG"; then
  bad "helper touched the remote credential directly"
else
  ok
fi
if grep -Fq 'not a credential' "$TMP"/*.out; then
  bad "helper output contains the credential fixture"
else
  ok
fi
if grep -Eq '^[[:space:]]*set[[:space:]]+-[a-z]*x' "$HELPER"; then
  bad "helper enables shell tracing"
else
  ok
fi

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
