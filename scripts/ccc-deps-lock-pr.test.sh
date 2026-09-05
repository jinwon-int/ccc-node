#!/usr/bin/env bash
# Tests for scripts/ccc-deps-lock-pr.sh (#1483) — no network: the lock script
# is a stub that rewrites pins on demand, `gh` is a stub that logs its argv,
# and `origin` is a local bare repository so `git push` is exercised for real.
# shellcheck disable=SC2034  # variables are read inside the eval'd conditions
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/ccc-deps-lock-pr.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); }
fail() { echo "FAIL: $*" >&2; FAIL=$((FAIL + 1)); }
check() { # <label> <shell condition>
  if eval "$2"; then pass; else fail "$1"; fi
}

mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG:?}"
case "${1:-} ${2:-}" in
  "pr list")
    case " $* " in
      *" --head "*) printf '%s\n' "${FAKE_EXISTING_PR:-}" ;;
      *) printf '%s\n' "${FAKE_STALE_PRS:-}" ;;
    esac ;;
  "pr create")
    # keep the body for assertions
    while [ "$#" -gt 0 ]; do
      [ "$1" = --body-file ] && cp "$2" "${FAKE_GH_BODY:?}"
      shift
    done
    printf 'https://github.com/example/repo/pull/%s\n' "${FAKE_NEW_PR:-77}" ;;
  "pr edit")
    while [ "$#" -gt 0 ]; do
      [ "$1" = --body-file ] && cp "$2" "${FAKE_GH_BODY:?}"
      shift
    done ;;
  "pr close"|"workflow run") ;;
  "api "*)
    case "$*" in
      *"/actions/runs?"*)
        # Nth list call; FAKE_GH_RUNS_AFTER=N hides the runs until call N
        # (polling test), default 1 = visible at once.
        n=$(( $(cat "${FAKE_GH_LIST_COUNT:?}" 2>/dev/null || echo 0) + 1 ))
        printf '%s\n' "$n" > "$FAKE_GH_LIST_COUNT"
        if [ "$n" -ge "${FAKE_GH_RUNS_AFTER:-1}" ]; then printf '%s\n' "${FAKE_GH_RUNS:-}"; fi ;;
      *"/actions/runs/"*"/approve"*)
        if [ "${FAKE_GH_APPROVE_RC:-0}" -ne 0 ]; then
          echo "gh: Resource not accessible by integration (HTTP 403)" >&2
          exit "$FAKE_GH_APPROVE_RC"
        fi ;;
      *) echo "unexpected fake gh api invocation: $*" >&2; exit 90 ;;
    esac ;;
  *) echo "unexpected fake gh invocation: $*" >&2; exit 90 ;;
esac
SH
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"

# Lock-script stub: records argv; FAKE_LOCK_MODE=bump moves ruff in both locks,
# adds a package to the CI lock and re-pins the fallback; noop leaves the tree
# untouched; fail exits 1.
cat > "$TMP/lock.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" > "${FAKE_LOCK_ARGS:?}"
case "${FAKE_LOCK_MODE:-noop}" in
  noop) ;;
  fail) echo "boom" >&2; exit 1 ;;
  bump)
    sed -i 's/^ruff==0.1.0 /ruff==0.2.0 /' .github/requirements/bridge-ci.txt
    sed -i 's/^Pydantic\[email\]==2.0.0 /Pydantic[email]==2.1.0 /' .github/requirements/bridge-ci.txt bridge/requirements.lock.txt
    printf 'newpkg==9.9.9 \\\n    --hash=sha256:cafe\n' >> .github/requirements/bridge-ci.txt
    sed -i 's/^pydantic==2.0.0$/pydantic==2.1.0/' bridge/requirements.txt ;;
esac
SH
chmod +x "$TMP/lock.sh"

fresh_repo() { # <name> -> prints work-tree path; origin is a local bare repo
  local name="$1" bare="$TMP/$1.git" work="$TMP/$1"
  rm -rf "$bare" "$work"
  git init -q --bare "$bare"
  git init -q -b main "$work"
  git -C "$work" config user.name test
  git -C "$work" config user.email test@example.invalid
  mkdir -p "$work/.github/requirements" "$work/bridge" "$work/scripts"
  cp "$SCRIPT" "$work/scripts/ccc-deps-lock-pr.sh"
  cat > "$work/.github/requirements/bridge-ci.txt" <<'LOCK'
# fake CI lock
ruff==0.1.0 \
    --hash=sha256:aaaa
Pydantic[email]==2.0.0 \
    --hash=sha256:bbbb
pytest==8.0.0 \
    --hash=sha256:cccc
LOCK
  cat > "$work/bridge/requirements.lock.txt" <<'LOCK'
# fake runtime lock
Pydantic[email]==2.0.0 \
    --hash=sha256:bbbb
LOCK
  printf '# fallback\npydantic==2.0.0\n' > "$work/bridge/requirements.txt"
  git -C "$work" add -A
  git -C "$work" commit -q -m "init"
  git -C "$work" remote add origin "$bare"
  git -C "$work" push -q origin main
  printf '%s\n' "$work"
}

run() { # <repo> <args...>  (stdout+stderr -> $TMP/out, rc -> $rc, job summary -> $TMP/summary.md)
  local work="$1"; shift
  export FAKE_GH_LOG="$TMP/gh.log" FAKE_GH_BODY="$TMP/body.md" FAKE_LOCK_ARGS="$TMP/lock.args" \
    FAKE_GH_LIST_COUNT="$TMP/gh.list-count"
  : > "$FAKE_GH_LOG"; : > "$TMP/summary.md"; rm -f "$FAKE_GH_BODY" "$FAKE_LOCK_ARGS" "$FAKE_GH_LIST_COUNT"
  # One look at the pull_request runs, no waiting, unless a case sets its own.
  (cd "$work" && CCC_DEPS_LOCK_SCRIPT="$TMP/lock.sh" CCC_DEPS_LOCK_PR_DATE=20260905 \
      CCC_DEPS_LOCK_PR_APPROVE_WAIT="${CCC_DEPS_LOCK_PR_APPROVE_WAIT-0}" \
      CCC_DEPS_LOCK_PR_APPROVE_POLL="${CCC_DEPS_LOCK_PR_APPROVE_POLL-1}" \
      GITHUB_STEP_SUMMARY="$TMP/summary.md" \
      bash scripts/ccc-deps-lock-pr.sh "$@") > "$TMP/out" 2>&1
  rc=$?
}

# --- 1. no changes: exit 0, default mode is --upgrade, nothing pushed ------
W="$(fresh_repo noop)"
FAKE_LOCK_MODE=noop run "$W"
check "noop exits 0" '[ "$rc" -eq 0 ]'
check "noop reports no lock changes" 'grep -q "no lock changes" "$TMP/out"'
check "default mode passes --upgrade to the lock script" '[ "$(cat "$TMP/lock.args")" = "--upgrade" ]'
check "noop calls no gh" '[ ! -s "$TMP/gh.log" ]'
check "noop pushes no branch" '! git -C "$TMP/noop.git" show-ref --verify -q refs/heads/deps/lock-pair-20260905'
check "noop stays on main" '[ "$(git -C "$W" branch --show-current)" = main ]'

# --- 2. bump, no existing PR: branch, commit, push, create, dispatch --------
W="$(fresh_repo bump)"
FAKE_LOCK_MODE=bump FAKE_EXISTING_PR="" FAKE_STALE_PRS="" run "$W" --upgrade "ruff==0.2.0 Pydantic"
check "bump exits 0" '[ "$rc" -eq 0 ]'
check "targeted list becomes --upgrade-package pairs" \
  '[ "$(cat "$TMP/lock.args")" = "--upgrade-package ruff==0.2.0 --upgrade-package Pydantic" ]'
check "branch pushed to origin" 'git -C "$TMP/bump.git" show-ref --verify -q refs/heads/deps/lock-pair-20260905'
HEAD_SHA="$(git -C "$W" rev-parse HEAD)"
check "pushed head equals local head" '[ "$(git -C "$TMP/bump.git" rev-parse refs/heads/deps/lock-pair-20260905)" = "$HEAD_SHA" ]'
check "commit carries exactly the lock set" \
  '[ "$(git -C "$W" show --format= --name-only HEAD | LC_ALL=C sort | tr "\n" " ")" = ".github/requirements/bridge-ci.txt bridge/requirements.lock.txt bridge/requirements.txt " ]'
MSG="$(git -C "$W" log -1 --format=%B)"
check "commit subject names the date" 'grep -q "^deps: regenerate lock pair (2026-09-05)" <<<"$MSG"'
check "commit lists moved pins" 'grep -q "ruff 0.1.0 -> 0.2.0" <<<"$MSG" && grep -q "pydantic 2.0.0 -> 2.1.0" <<<"$MSG" && grep -q "newpkg - -> 9.9.9" <<<"$MSG"'
check "commit references the tracking issue" 'grep -q "Refs #1483" <<<"$MSG"'
check "commit records targeted mode" 'grep -q "Mode: targeted: ruff==0.2.0 Pydantic" <<<"$MSG"'
check "pr create against main with the bot branch" 'grep -q "^pr create --base main --head deps/lock-pair-20260905 --title deps: regenerate lock pair (2026-09-05) --body-file " "$TMP/gh.log"'
check "no pr edit on the create path" '! grep -q "^pr edit" "$TMP/gh.log"'
check "body has the pin table rows" 'grep -q "| ruff | \`.github/requirements/bridge-ci.txt\` | 0.1.0 | 0.2.0 |" "$TMP/body.md" && grep -q "| pydantic | \`bridge/requirements.lock.txt\` | 2.0.0 | 2.1.0 |" "$TMP/body.md"'
check "body explains the GITHUB_TOKEN dispatch and refs the issue" 'grep -q "GITHUB_TOKEN" "$TMP/body.md" && grep -q "workflow_dispatch" "$TMP/body.md" && grep -q "Refs #1483" "$TMP/body.md"'
check "ci.yml dispatched on the branch" 'grep -qx "workflow run ci.yml --ref deps/lock-pair-20260905" "$TMP/gh.log"'
check "codeql.yml dispatched on the branch" 'grep -qx "workflow run codeql.yml --ref deps/lock-pair-20260905" "$TMP/gh.log"'
check "dispatch happens after the PR exists" '[ "$(grep -n "^pr create" "$TMP/gh.log" | cut -d: -f1)" -lt "$(grep -n "^workflow run ci.yml" "$TMP/gh.log" | cut -d: -f1)" ]'
check "summary names the PR and head" 'grep -q "PR #77 head ${HEAD_SHA:0:12}" "$TMP/out"'

# --- 3. same-day rerun: existing PR updated, branch force-refreshed, stale closed
FIRST_SHA="$HEAD_SHA"
git -C "$W" switch -q main
FAKE_LOCK_MODE=bump FAKE_EXISTING_PR=55 FAKE_STALE_PRS=$'41\n42' run "$W"
check "rerun exits 0" '[ "$rc" -eq 0 ]'
check "rerun edits the existing PR" 'grep -q "^pr edit 55 --title deps: regenerate lock pair (2026-09-05) --body-file " "$TMP/gh.log"'
check "rerun does not create a PR" '! grep -q "^pr create" "$TMP/gh.log"'
check "rerun force-refreshes the branch (new head, single commit over main)" \
  '[ "$(git -C "$TMP/bump.git" rev-parse refs/heads/deps/lock-pair-20260905)" != "$FIRST_SHA" ] && [ "$(git -C "$W" rev-list --count main..HEAD)" -eq 1 ]'
check "stale bot PRs closed as superseded" 'grep -q "^pr close 41 --comment Superseded by #55" "$TMP/gh.log" && grep -q "^pr close 42 --comment Superseded by #55" "$TMP/gh.log"'
check "rerun records upgrade-all mode" 'git -C "$W" log -1 --format=%B | grep -q "Mode: upgrade-all"'

# --- 4. dry run: regenerate + table, no branch/push/gh ---------------------
W="$(fresh_repo dry)"
FAKE_LOCK_MODE=bump run "$W" --dry-run
check "dry run exits 0" '[ "$rc" -eq 0 ]'
check "dry run prints the table" 'grep -q "| ruff | \`.github/requirements/bridge-ci.txt\` | 0.1.0 | 0.2.0 |" "$TMP/out"'
check "dry run leaves the work tree modified" '[ -n "$(git -C "$W" status --porcelain)" ]'
check "dry run stays on main" '[ "$(git -C "$W" branch --show-current)" = main ]'
check "dry run pushes nothing" '! git -C "$TMP/dry.git" show-ref --verify -q refs/heads/deps/lock-pair-20260905'
check "dry run calls no gh" '[ ! -s "$TMP/gh.log" ]'

# --- 5. lock script failure aborts before any git/gh action ----------------
W="$(fresh_repo fail)"
FAKE_LOCK_MODE=fail run "$W"
check "lock failure propagates" '[ "$rc" -ne 0 ]'
check "lock failure calls no gh" '[ ! -s "$TMP/gh.log" ]'
check "lock failure leaves main clean" '[ -z "$(git -C "$W" status --porcelain)" ]'

# --- 6. guards -------------------------------------------------------------
W="$(fresh_repo dirty)"
printf 'dirty\n' >> "$W/bridge/requirements.txt"
FAKE_LOCK_MODE=noop run "$W"
check "pre-modified lock set is refused" '[ "$rc" -ne 0 ] && grep -q "already modified" "$TMP/out"'
check "refusal runs no lock script" '[ ! -e "$TMP/lock.args" ]'

W="$(fresh_repo prefix)"
FAKE_LOCK_MODE=noop run "$W" --branch feature/not-a-bot-branch
check "force-push outside the bot prefix is refused" '[ "$rc" -eq 2 ] && grep -q "bot prefix" "$TMP/out"'

FAKE_LOCK_MODE=noop run "$W" --upgrade "--upgrade"
check "flag-shaped upgrade entries are rejected" '[ "$rc" -eq 2 ] && grep -q "not flags" "$TMP/out"'

W="$(fresh_repo nodispatch)"
FAKE_LOCK_MODE=bump CCC_DEPS_LOCK_PR_DISPATCH="" run "$W"
check "empty dispatch list skips workflow runs" '[ "$rc" -eq 0 ] && ! grep -q "^workflow run" "$TMP/gh.log" && grep -q "^pr create" "$TMP/gh.log"'

# --- 7. approving the PR's own action_required pull_request runs (#1483) ---
# (a) every action_required run the API reports for the head is approved
W="$(fresh_repo approve)"
FAKE_LOCK_MODE=bump FAKE_GH_RUNS=$'900\t.github/workflows/ci.yml\n901\t.github/workflows/codeql.yml' run "$W"
HEAD_SHA="$(git -C "$W" rev-parse HEAD)"
check "approve: exits 0" '[ "$rc" -eq 0 ]'
check "approve: lists pull_request runs on the bot branch" \
  'grep -q "^api repos/{owner}/{repo}/actions/runs?branch=deps/lock-pair-20260905&event=pull_request&per_page=50 --jq " "$TMP/gh.log"'
check "approve: list filters on the pushed head SHA and action_required" \
  'grep "^api repos/{owner}/{repo}/actions/runs?" "$TMP/gh.log" | grep -q "head_sha == \"$HEAD_SHA\" and .conclusion == \"action_required\""'
check "approve: POSTs /approve for each run id" \
  'grep -qx "api -X POST repos/{owner}/{repo}/actions/runs/900/approve" "$TMP/gh.log" && grep -qx "api -X POST repos/{owner}/{repo}/actions/runs/901/approve" "$TMP/gh.log"'
check "approve: exactly two approve calls" '[ "$(grep -c "/approve$" "$TMP/gh.log")" -eq 2 ]'
check "approve: runs after the dispatch step" \
  '[ "$(grep -n "^workflow run codeql.yml" "$TMP/gh.log" | cut -d: -f1)" -lt "$(grep -n "/actions/runs/900/approve" "$TMP/gh.log" | cut -d: -f1)" ]'
check "approve: log names each approved run" 'grep -q "approved pull_request run 900 (.github/workflows/ci.yml)" "$TMP/out" && grep -q "approved pull_request run 901 (.github/workflows/codeql.yml)" "$TMP/out"'
check "approve: job summary records OK per run" 'grep -q "^#### pull_request run approval (#1483)" "$TMP/summary.md" && grep -q "approved run 900 (\`.github/workflows/ci.yml\`): OK" "$TMP/summary.md" && grep -q "approved run 901" "$TMP/summary.md"'
check "approve: PR body documents the gate and the manual command" 'grep -q "action_required" "$TMP/body.md" && grep -q "actions/runs/<id>/approve" "$TMP/body.md"'

# (b) approval failure is non-fatal and prints the manual command
W="$(fresh_repo approve-fail)"
FAKE_LOCK_MODE=bump FAKE_GH_RUNS=$'900\t.github/workflows/ci.yml' FAKE_GH_APPROVE_RC=1 run "$W"
check "approve failure: still exits 0" '[ "$rc" -eq 0 ]'
check "approve failure: approve was attempted" 'grep -qx "api -X POST repos/{owner}/{repo}/actions/runs/900/approve" "$TMP/gh.log"'
check "approve failure: warns with the HTTP status" 'grep -q "could not approve pull_request run 900 (.github/workflows/ci.yml): rc=1 HTTP 403" "$TMP/out"'
check "approve failure: prints the manual command" 'grep -q "manual fallback: gh api -X POST repos/{owner}/{repo}/actions/runs/900/approve" "$TMP/out"'
check "approve failure: job summary says FAILED + manual command" 'grep -q "approve run 900 (\`.github/workflows/ci.yml\`) FAILED (rc=1 HTTP 403)" "$TMP/summary.md" && grep -q "actions/runs/900/approve\` manually" "$TMP/summary.md"'
check "approve failure: final success line still printed" 'grep -q "^✅ deps-lock-pr: PR #77" "$TMP/out"'

# (c) no action_required run -> no approve call
W="$(fresh_repo approve-none)"
FAKE_LOCK_MODE=bump FAKE_GH_RUNS="" run "$W"
check "no pending runs: exits 0" '[ "$rc" -eq 0 ]'
check "no pending runs: runs were listed" 'grep -q "^api repos/{owner}/{repo}/actions/runs?" "$TMP/gh.log"'
check "no pending runs: no approve call" '! grep -q "/approve$" "$TMP/gh.log"'
check "no pending runs: log + summary say nothing to approve" 'grep -q "nothing to approve" "$TMP/out" && grep -q "nothing approved" "$TMP/summary.md"'

# (d) runs that appear late are picked up by the bounded poll
W="$(fresh_repo approve-poll)"
FAKE_LOCK_MODE=bump FAKE_GH_RUNS=$'900\t.github/workflows/ci.yml\n901\t.github/workflows/codeql.yml' FAKE_GH_RUNS_AFTER=2 \
  CCC_DEPS_LOCK_PR_APPROVE_WAIT=5 CCC_DEPS_LOCK_PR_APPROVE_POLL=1 run "$W"
check "poll: exits 0" '[ "$rc" -eq 0 ]'
check "poll: listed more than once" '[ "$(cat "$TMP/gh.list-count")" -ge 2 ]'
check "poll: late runs approved" '[ "$(grep -c "/approve$" "$TMP/gh.log")" -eq 2 ]'

# (e) skip knob: no list, no approve; bad value refused
W="$(fresh_repo approve-skip)"
FAKE_LOCK_MODE=bump FAKE_GH_RUNS=$'900\t.github/workflows/ci.yml' CCC_DEPS_LOCK_PR_APPROVE_WAIT=skip run "$W"
check "skip: exits 0 without any api call" '[ "$rc" -eq 0 ] && ! grep -q "^api " "$TMP/gh.log" && grep -q "runs skipped (CCC_DEPS_LOCK_PR_APPROVE_WAIT=skip)" "$TMP/summary.md"'
W="$(fresh_repo approve-bad)"
FAKE_LOCK_MODE=bump CCC_DEPS_LOCK_PR_APPROVE_WAIT=soon run "$W"
check "bad wait value: refused before regeneration (exit 2, no lock script, no gh)" '[ "$rc" -eq 2 ] && [ ! -e "$TMP/lock.args" ] && [ ! -s "$TMP/gh.log" ]'

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
