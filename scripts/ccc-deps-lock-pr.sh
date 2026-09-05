#!/usr/bin/env bash
# Regenerate the dependency lock pair and open/refresh the bot pull request
# (issue #1483; runs from .github/workflows/deps-lock.yml).
#
# Why this exists: .github/requirements/bridge-ci.txt and
# bridge/requirements.lock.txt are ONE lock pair derived from
# bridge/pyproject.toml by scripts/ccc-deps-lock.sh (the runtime lock is
# compiled with the CI lock as a pip constraint). Dependabot cannot run that
# derivation and its groups do not span directories, so every Dependabot pip
# PR moved a pin in one lock only and failed tests/test_runtime_deps_lock.py
# (#1453-#1457, #1495). This script replaces those PRs with one bot PR whose
# locks were regenerated together.
#
# Flow:
#   1. run scripts/ccc-deps-lock.sh (default: --upgrade everything the inputs
#      allow; --upgrade "<pkg[==ver] ...>" restricts the move to those names)
#   2. no diff in the lock set -> print "no lock changes" and exit 0
#   3. otherwise commit the lock set on deps/lock-pair-<YYYYMMDD> (branch is
#      created or force-refreshed — it is bot-owned, force-push is confined to
#      this prefix), push, open-or-update the PR against --base, close any
#      older open deps/lock-pair-* PR as superseded
#   4. dispatch CI on the branch: pushes and PRs made with GITHUB_TOKEN never
#      trigger `pull_request`/`push` workflows, but `workflow_dispatch` does,
#      and the resulting check runs attach to the head SHA, so the required
#      contexts (.github/required-checks.json) still gate the bot PR.
#   5. approve the PR's own `pull_request` runs: on the first two bot PRs
#      (#1505, heads 2f66c3f/d1dd911) GitHub DID register `pull_request` runs
#      of harness-ci and codeql for the github-actions[bot] author, but parked
#      them at conclusion=action_required (workflow-approval gate, repository
#      policy `first_time_contributors`). Their check suites then counted as
#      incomplete and the PR stayed mergeStateStatus=BLOCKED even though the
#      dispatched required checks were green — until a human POSTed
#      /actions/runs/<id>/approve on both (runs 33962069430/33962069445 and
#      33963658260/33963658264). This step polls for such runs on the new head
#      and approves them with the job's GITHUB_TOKEN (actions: write). See
#      approve_pending_runs for what is and is not verified about that.
#
# Everything network-facing goes through `git push` and `gh`, so a test can
# point the remote at a local bare repo and put a stub `gh` on PATH.
set -euo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: scripts/ccc-deps-lock-pr.sh [--upgrade "<pkg[==ver]> ..."] [--base BRANCH]
                                   [--branch NAME] [--dry-run]

  --upgrade LIST  whitespace-separated packages (pkg or pkg==ver) that may
                  move; each becomes --upgrade-package for ccc-deps-lock.sh.
                  Empty/omitted = plain --upgrade (every pin may move).
  --base BRANCH   pull-request base (default: main)
  --branch NAME   head branch (default: deps/lock-pair-<YYYYMMDD>, UTC)
  --dry-run       regenerate and print the pin diff; no branch/push/PR/dispatch

environment:
  CCC_DEPS_LOCK_SCRIPT        lock script (default: scripts/ccc-deps-lock.sh)
  CCC_DEPS_LOCK_PR_DATE       YYYYMMDD used in the default branch name
  CCC_DEPS_LOCK_PR_REMOTE     git remote to push to (default: origin)
  CCC_DEPS_LOCK_PR_DISPATCH   workflows to dispatch on the branch
                              (default: "ci.yml codeql.yml"; empty = none)
  CCC_DEPS_LOCK_PR_ISSUE      tracking issue number (default: 1483)
  CCC_DEPS_LOCK_PR_APPROVE_WAIT
                              seconds to wait for the PR's own `pull_request`
                              runs to appear in action_required before
                              approving them (default: 90; 0 = one look, no
                              wait; "skip" = do not approve at all)
  CCC_DEPS_LOCK_PR_APPROVE_POLL
                              poll interval in seconds (default: 5)
  GH_TOKEN / GH_REPO          consumed by gh as usual
USAGE
}

UPGRADE_LIST=""
UPGRADE_SET=0
BASE="main"
BRANCH=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --upgrade)
            [ "$#" -ge 2 ] || { echo "❌ --upgrade requires a value (may be empty)" >&2; exit 2; }
            UPGRADE_LIST="$2"; UPGRADE_SET=1; shift 2 ;;
        --upgrade=*) UPGRADE_LIST="${1#*=}"; UPGRADE_SET=1; shift ;;
        --base)
            [ "$#" -ge 2 ] || { echo "❌ --base requires a branch" >&2; exit 2; }
            BASE="$2"; shift 2 ;;
        --base=*) BASE="${1#*=}"; shift ;;
        --branch)
            [ "$#" -ge 2 ] || { echo "❌ --branch requires a name" >&2; exit 2; }
            BRANCH="$2"; shift 2 ;;
        --branch=*) BRANCH="${1#*=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "❌ unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done
[ "$UPGRADE_SET" -eq 1 ] || UPGRADE_LIST="${CCC_DEPS_LOCK_PR_UPGRADE:-}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

LOCK_SCRIPT="${CCC_DEPS_LOCK_SCRIPT:-$REPO_ROOT/scripts/ccc-deps-lock.sh}"
REMOTE="${CCC_DEPS_LOCK_PR_REMOTE:-origin}"
DISPATCH="${CCC_DEPS_LOCK_PR_DISPATCH-ci.yml codeql.yml}"
ISSUE="${CCC_DEPS_LOCK_PR_ISSUE:-1483}"
APPROVE_WAIT="${CCC_DEPS_LOCK_PR_APPROVE_WAIT:-90}"
APPROVE_POLL="${CCC_DEPS_LOCK_PR_APPROVE_POLL:-5}"
BRANCH_PREFIX="deps/lock-pair-"
DATE_UTC="${CCC_DEPS_LOCK_PR_DATE:-$(date -u +%Y%m%d)}"
[ -n "$BRANCH" ] || BRANCH="${BRANCH_PREFIX}${DATE_UTC}"

# The lock pair plus the exact-pin mirror that the unlocked fallback keeps in
# step with the runtime lock (tests/test_runtime_deps_lock.py).
CI_LOCK=".github/requirements/bridge-ci.txt"
RUNTIME_LOCK="bridge/requirements.lock.txt"
FALLBACK="bridge/requirements.txt"
LOCK_SET=("$CI_LOCK" "$RUNTIME_LOCK" "$FALLBACK")

[ -x "$LOCK_SCRIPT" ] || [ -f "$LOCK_SCRIPT" ] || { echo "❌ lock script not found: $LOCK_SCRIPT" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "❌ not inside a git work tree: $REPO_ROOT" >&2; exit 1; }
if [ -n "$(git status --porcelain -- "${LOCK_SET[@]}")" ]; then
    echo "❌ lock set already modified before regeneration; commit or discard first:" >&2
    git status --short -- "${LOCK_SET[@]}" >&2
    exit 1
fi
case "$BRANCH" in
    "$BRANCH_PREFIX"*) ;;
    *) echo "❌ refusing to force-push outside the bot prefix ${BRANCH_PREFIX}*: $BRANCH" >&2; exit 2 ;;
esac
case "$APPROVE_WAIT/$APPROVE_POLL" in
    skip/*|[0-9]*/[0-9]*) ;;
    *) echo "❌ CCC_DEPS_LOCK_PR_APPROVE_WAIT must be seconds or 'skip' and _POLL seconds: $APPROVE_WAIT / $APPROVE_POLL" >&2; exit 2 ;;
esac

BASE_SHA="$(git rev-parse HEAD)"

# --- 1. regenerate --------------------------------------------------------
LOCK_ARGS=()
MODE_LABEL=""
if [ -n "${UPGRADE_LIST// /}" ]; then
    # shellcheck disable=SC2086 # intentional word split of the operator list
    set -- $UPGRADE_LIST
    for spec in "$@"; do
        case "$spec" in
            -*) echo "❌ --upgrade entries are package specs, not flags: $spec" >&2; exit 2 ;;
        esac
        LOCK_ARGS+=(--upgrade-package "$spec")
    done
    MODE_LABEL="targeted: $*"
else
    LOCK_ARGS=(--upgrade)
    MODE_LABEL="upgrade-all (every pin bridge/pyproject.toml permits may move)"
fi

echo "== deps-lock-pr: base=$BASE@${BASE_SHA:0:12} branch=$BRANCH mode=$MODE_LABEL =="
bash "$LOCK_SCRIPT" "${LOCK_ARGS[@]}"

# --- 2. detect changes ----------------------------------------------------
CHANGED_FILES="$(git diff --name-only -- "${LOCK_SET[@]}")"
if [ -z "$CHANGED_FILES" ]; then
    echo "== deps-lock-pr: no lock changes (lock pair already current) =="
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        printf '### deps-lock\n\nNo lock changes (mode: %s).\n' "$MODE_LABEL" >> "$GITHUB_STEP_SUMMARY"
    fi
    exit 0
fi

# `name version` per pin line; extras are stripped and names lower-cased so
# `Foo[bar]==1` and `foo==1` compare as the same package.
pins() {
    awk '/^[A-Za-z0-9_.-]+(\[[^]]*\])?==/ {
        n = $0; sub(/(\[[^]]*\])?==.*/, "", n)
        v = $0; sub(/^[^=]*==/, "", v); sub(/[ \t\\;].*/, "", v)
        print tolower(n), v
    }'
}

# `name before after` for every pin that differs (added: before="-", removed:
# after="-"), sorted by name.
pin_diff() { # <before-file> <after-file>
    awk '
        FILENAME == ARGV[1] { before[$1] = $2; next }
        { after[$1] = $2 }
        END {
            for (n in before) if (!(n in after)) print n, before[n], "-"
            for (n in after) {
                if (!(n in before)) print n, "-", after[n]
                else if (before[n] != after[n]) print n, before[n], after[n]
            }
        }' "$1" "$2" | LC_ALL=C sort
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ccc-deps-lock-pr.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
: > "$WORK/rows.tsv"
for lock in "$CI_LOCK" "$RUNTIME_LOCK"; do
    git show "HEAD:$lock" 2>/dev/null | pins > "$WORK/before.txt" || : > "$WORK/before.txt"
    pins < "$lock" > "$WORK/after.txt"
    pin_diff "$WORK/before.txt" "$WORK/after.txt" | awk -v lock="$lock" '{ print $1 "\t" lock "\t" $2 "\t" $3 }' >> "$WORK/rows.tsv"
done

# One line per package for the commit message (first lock that reports it),
# and a markdown table for the PR body.
CHANGED_PINS="$(awk -F'\t' '!seen[$1]++ { printf "%s %s -> %s\n", $1, $3, $4 }' "$WORK/rows.tsv")"
TABLE="$(printf '| Package | Lock | Before | After |\n|---|---|---|---|\n'; awk -F'\t' '{ printf "| %s | `%s` | %s | %s |\n", $1, $2, $3, $4 }' "$WORK/rows.tsv")"
PIN_COUNT="$(awk -F'\t' '!seen[$1]++ { c++ } END { print c + 0 }' "$WORK/rows.tsv")"

echo "== deps-lock-pr: $PIN_COUNT pin(s) changed =="
printf '%s\n' "$CHANGED_PINS"
printf 'changed files:\n%s\n' "$CHANGED_FILES"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "== deps-lock-pr: dry run — lock set left modified in the work tree, nothing pushed =="
    printf '%s\n' "$TABLE"
    exit 0
fi

# --- 3. branch, commit, push, PR ------------------------------------------
DATE_ISO="${DATE_UTC:0:4}-${DATE_UTC:4:2}-${DATE_UTC:6:2}"
TITLE="deps: regenerate lock pair ($DATE_ISO)"
COMMIT_MSG="$(cat <<EOF
$TITLE

Regenerated by scripts/ccc-deps-lock-pr.sh (deps-lock workflow) from
bridge/pyproject.toml; both hash locks and the bridge/requirements.txt
mirror move together so tests/test_runtime_deps_lock.py stays green.
Mode: $MODE_LABEL
Base: $BASE@${BASE_SHA:0:12}

Changed pins:
$(printf '%s\n' "$CHANGED_PINS" | sed 's/^/  /')

Refs #$ISSUE
EOF
)"

git switch -C "$BRANCH" >/dev/null 2>&1 || git checkout -B "$BRANCH"
git add -- "${LOCK_SET[@]}"
git commit -q -m "$COMMIT_MSG"
HEAD_SHA="$(git rev-parse HEAD)"
# Force-push is confined to the bot prefix (checked above): a same-day rerun
# refreshes the branch in place instead of stacking commits.
git push --force "$REMOTE" "HEAD:refs/heads/$BRANCH"
echo "== deps-lock-pr: pushed $BRANCH@${HEAD_SHA:0:12} to $REMOTE =="

DISPATCH_NOTE="none"
[ -n "${DISPATCH// /}" ] && DISPATCH_NOTE="$(printf '%s' "$DISPATCH" | tr -s ' ' ',' | sed 's/,/`, `/g')"
BODY_FILE="$WORK/body.md"
cat > "$BODY_FILE" <<EOF
## Lock-pair regeneration ($DATE_ISO)

\`scripts/ccc-deps-lock.sh\` re-derived \`$CI_LOCK\` and \`$RUNTIME_LOCK\` from \`bridge/pyproject.toml\` (mode: $MODE_LABEL) on top of \`$BASE\` @ \`${BASE_SHA:0:12}\`; \`$FALLBACK\` mirrors the runtime pins.

$TABLE

Both locks move together in this one commit, which is what per-directory Dependabot pip PRs could not do (#$ISSUE) — \`tests/test_runtime_deps_lock.py\` verifies the pair.

### CI

Pushes and pull requests made with \`GITHUB_TOKEN\` do not trigger \`pull_request\`/\`push\` workflows, so the workflow dispatched $DISPATCH_NOTE on \`$BRANCH\` via \`workflow_dispatch\`; those check runs attach to the head SHA and satisfy the required contexts. If the checks are missing, re-run \`gh workflow run deps-lock.yml\` (same-day reruns refresh this branch) or dispatch the workflows on this ref manually.

GitHub may still register this PR's own \`pull_request\` runs and park them at **action_required** (workflow-approval gate for the bot author), which keeps the PR \`BLOCKED\` even with green required checks. The workflow tries to approve those runs itself (see its job summary); if that failed, approve them manually — \`gh run list --branch $BRANCH --event pull_request\`, then \`gh api -X POST repos/{owner}/{repo}/actions/runs/<id>/approve\` per run — and merge after they complete (\`docs/ci-governance.md\`, "Weekly lock-pair regeneration").

Refs #$ISSUE
EOF

EXISTING="$(gh pr list --state open --base "$BASE" --head "$BRANCH" --json number --jq '.[0].number // empty')"
if [ -n "$EXISTING" ]; then
    gh pr edit "$EXISTING" --title "$TITLE" --body-file "$BODY_FILE" >/dev/null
    PR_NUMBER="$EXISTING"
    echo "== deps-lock-pr: updated PR #$PR_NUMBER =="
else
    PR_URL="$(gh pr create --base "$BASE" --head "$BRANCH" --title "$TITLE" --body-file "$BODY_FILE")"
    PR_NUMBER="${PR_URL##*/}"
    echo "== deps-lock-pr: opened PR #$PR_NUMBER ($PR_URL) =="
fi

# Older bot PRs (a previous week's branch) are superseded by this one.
STALE="$(gh pr list --state open --base "$BASE" --limit 100 --json number,headRefName \
    --jq ".[] | select(.headRefName | startswith(\"$BRANCH_PREFIX\")) | select(.headRefName != \"$BRANCH\") | .number")"
for stale in $STALE; do
    [ "$stale" = "$PR_NUMBER" ] && continue
    gh pr close "$stale" --comment "Superseded by #$PR_NUMBER (newer lock-pair regeneration)." --delete-branch >/dev/null \
        && echo "== deps-lock-pr: closed superseded PR #$stale ==" \
        || echo "⚠ could not close superseded PR #$stale (continuing)" >&2
done

# --- 4. trigger CI on the bot branch --------------------------------------
for wf in $DISPATCH; do
    gh workflow run "$wf" --ref "$BRANCH"
    echo "== deps-lock-pr: dispatched $wf on $BRANCH =="
done

# --- 5. approve the PR's own action_required `pull_request` runs ----------
# `<run id>\t<workflow path>` for every `pull_request` run on this branch whose
# head is our commit and which is parked at the approval gate. Such a run is
# status=completed/conclusion=action_required with zero jobs (verified on run
# 33961834862): nothing in the workflow — not even a job-level `if:` — is
# evaluated before approval, which is why skipping bot runs inside ci.yml /
# codeql.yml cannot avoid the gate.
pending_pull_request_runs() {
    gh api "repos/{owner}/{repo}/actions/runs?branch=$BRANCH&event=pull_request&per_page=50" \
        --jq ".workflow_runs[] | select(.head_sha == \"$HEAD_SHA\" and .conclusion == \"action_required\") | \"\(.id)\t\(.path)\""
}

# Never fatal: the dispatched runs (step 4) already carry the required
# contexts, so a failed approval leaves only mergeability blocked, and the
# manual command is printed for the reviewer. The approve endpoint is
# documented for pull requests from public forks
# (POST /repos/{owner}/{repo}/actions/runs/{run_id}/approve, fine-grained
# permission actions: write, which deps-lock.yml already grants for
# `gh workflow run`). Whether GitHub accepts it from GITHUB_TOKEN for a
# same-repository PR authored by github-actions[bot] — i.e. the bot approving
# its own run — is NOT documented; the finalizer validates it on the next real
# deps-lock dispatch and records the outcome on issue #$ISSUE. Until then this
# step's job-summary lines (HTTP status per run) are the evidence either way.
APPROVE_NOTE=""
approve_pending_runs() {
    local deadline now runs expected_n found_n id path out rc msg
    expected_n="$(printf '%s\n' $DISPATCH | grep -c .)" || expected_n=0
    now="$(date +%s)"; deadline=$((now + APPROVE_WAIT))
    runs=""
    while :; do
        runs="$(pending_pull_request_runs 2>&1)" || { APPROVE_NOTE="- could not list workflow runs: ${runs//$'\n'/ } — approve manually if the PR stays BLOCKED"; return 0; }
        found_n="$(printf '%s\n' "$runs" | grep -c .)" || found_n=0
        [ "$found_n" -ge "$expected_n" ] && break
        now="$(date +%s)"; [ "$now" -ge "$deadline" ] && break
        sleep "$APPROVE_POLL"
    done
    if [ "$found_n" -eq 0 ]; then
        echo "== deps-lock-pr: no action_required pull_request run on ${HEAD_SHA:0:12} after ${APPROVE_WAIT}s (nothing to approve) =="
        APPROVE_NOTE="- no \`pull_request\` run was parked at action_required on \`${HEAD_SHA:0:12}\` within ${APPROVE_WAIT}s; nothing approved"
        return 0
    fi
    while IFS=$'\t' read -r id path; do
        [ -n "$id" ] || continue
        rc=0
        out="$(gh api -X POST "repos/{owner}/{repo}/actions/runs/$id/approve" 2>&1)" || rc=$?
        if [ "$rc" -eq 0 ]; then
            echo "== deps-lock-pr: approved pull_request run $id ($path) =="
            APPROVE_NOTE="$APPROVE_NOTE"$'\n'"- approved run $id (\`$path\`): OK"
        else
            # gh prints e.g. "gh: Must have admin rights ... (HTTP 403)"; keep the status.
            msg="$(printf '%s\n' "$out" | grep -m1 -o 'HTTP [0-9]\{3\}' || true)"
            [ -n "$msg" ] || msg="$(printf '%s\n' "$out" | head -1)"
            echo "⚠ could not approve pull_request run $id ($path): rc=$rc $msg" >&2
            echo "  manual fallback: gh api -X POST repos/{owner}/{repo}/actions/runs/$id/approve" >&2
            APPROVE_NOTE="$APPROVE_NOTE"$'\n'"- approve run $id (\`$path\`) FAILED (rc=$rc $msg) — run \`gh api -X POST repos/{owner}/{repo}/actions/runs/$id/approve\` manually, then merge"
        fi
    done <<< "$runs"
    APPROVE_NOTE="${APPROVE_NOTE#$'\n'}"
}

if [ "$APPROVE_WAIT" = skip ]; then
    echo "== deps-lock-pr: pull_request run approval skipped (CCC_DEPS_LOCK_PR_APPROVE_WAIT=skip) =="
    APPROVE_NOTE="- approval of \`pull_request\` runs skipped (CCC_DEPS_LOCK_PR_APPROVE_WAIT=skip)"
else
    approve_pending_runs
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
        printf '### deps-lock\n\nPR #%s on `%s` (`%s`), mode: %s\n\n' "$PR_NUMBER" "$BRANCH" "${HEAD_SHA:0:12}" "$MODE_LABEL"
        printf '%s\n\n' "$TABLE"
        printf '#### pull_request run approval (#%s)\n\n%s\n' "$ISSUE" "$APPROVE_NOTE"
    } >> "$GITHUB_STEP_SUMMARY"
fi
echo "✅ deps-lock-pr: PR #$PR_NUMBER head ${HEAD_SHA:0:12} ($PIN_COUNT pin(s) changed)"
