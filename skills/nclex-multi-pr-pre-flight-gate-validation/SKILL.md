---
name: nclex-multi-pr-pre-flight-gate-validation
description: Pre-flight validate one or many jinwon-int/nclex content PRs against the CURRENT main gate before A2A dispatch or rebase — pin each head in a detached worktree, replay the gate job's validation steps, reproduce the stale-base trap that makes an old green CI turn red on push, classify failures per tool (validate_refs, ko_terminology, ko_coverage, content_process/coverage, a2a_quorum, similarity), and emit READY / NEEDS_FIX / NEEDS_REBASE verdicts with preserved evidence. Use when triaging a batch of open nclex PRs, when a PR's merge base is weeks behind main, when a PR shows green checks but you suspect the rules moved, or before dispatching to nclex-a2a-content-pipeline.
---

# nclex multi-PR pre-flight gate validation

Answers one question per PR before any dispatch or merge work:
**"Does this head still pass the gate that main enforces *today*?"**

A PR's recorded green check only proves it passed against its own (possibly
stale) base. This skill re-derives the answer against current main.

Scope: validation and triage only. It never pushes, never comments, never
dispatches. Downstream work belongs to `nclex-a2a-content-pipeline`.

## Preconditions

- A clone of the content repo; run commands from its root (`REPO="$PWD"`).
- Toolchain matching CI: **Node 20**, **Python 3.12**. Record local versions —
  a version gap is a legitimate explanation for a local-only failure.
- Network access: one gate step fetches quorum head SHAs from origin.

## Procedure

### 1. Pin each head in a detached worktree

Never validate a PR by checking it out in the working tree.

```bash
REPO="$PWD"
WT="${TMPDIR:-/tmp}/nclex-preflight"
mkdir -p "$WT"
for PR in <PR#> <PR#> …; do
  SHA="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"
  git worktree add --detach "$WT/pr-$PR" "$SHA"
done
git fetch origin main            # baseline must be current
```

Record for each PR: `headRefOid`, `baseRefName`, `mergeable`,
`mergeStateStatus` (`gh pr view <PR#> --json number,title,headRefOid,baseRefName,mergeable,mergeStateStatus`).

### 2. Replay the gate job's validation steps

There is **no single `gate.js` entrypoint** — the gate is the step list in
`.github/workflows/gate.yml`, job `gate`. Replay the named steps in order:

```bash
cd "$WT/pr-$PR"
python3 tools/validate_refs.py                      # 참고자료 manifest 검증
node tools/ko_terminology.test.js && node tools/ko_terminology.js
node tools/ko_coverage.test.js  && node tools/ko_coverage.js --check
node tools/content_process.test.js && node tools/content_process.js \
  && python3 tools/coverage.test.py && python3 tools/coverage.py --check
node tools/validate.js && node tools/attributions.test.js
node tools/selftest.js
node tools/ui_mode.test.js
node tools/a2a_quorum.js --list-heads | while IFS= read -r sha; do
  git fetch --no-tags origin "$sha"; done
node tools/a2a_quorum.test.js && node tools/a2a_quorum.js --ci \
  && node tools/clinical_review_ui.test.js && node tools/paid_pool_policy.test.js \
  && node tools/commercial_gate_contract.test.js && node tools/free_pool_manifest.test.js \
  && node tools/a2a_dispatch.test.js && node tools/a2a_receipt_check.test.js
node tools/landing.test.js && node tools/landing-stamp.js --check \
  && node tools/consent_snapshot.test.js && python3 -m unittest api.test_server
python3 tools/similarity.py --gate
node tools/commercial_readiness.test.js && node tools/commercial_readiness.js
```

**Never run `tools/a2a_dispatch.js`.** It lives in a *separate* `a2a-dispatch`
job (`needs: gate`, PR events only) and it **posts** an evaluation request
comment. Its `*.test.js` is part of the gate; the tool itself is not.

Re-check the step list against `gate.yml` at the head being validated — the
gate grows, and a step added after the PR branched is exactly what this skill
exists to catch.

### 3. Baseline against current main — inside the worktree, then abort

Do **not** create a branch or merge in the primary working tree.

```bash
cd "$WT/pr-$PR"
git merge --no-commit --no-ff origin/main
git diff --name-only --diff-filter=U        # conflicting paths, if any
# …re-run step 2 here for the merged-tree result…
git merge --abort
```

Direction matters: merging **main into the head** models what the PR will
become. Record conflicting paths before aborting.

### 4. Reproduce the stale-base trap (the failure a green check hides)

`content_process.js` compares the head against the **PR base SHA supplied by
the event payload**, not against the merge base recorded when CI last ran
(`tools/content_process.js` reads `GITHUB_EVENT_NAME` / `GITHUB_EVENT_PATH`
and takes `event.pull_request.base.sha`). So a PR whose base is weeks old can
hold a green check and turn red the moment anything re-triggers CI.

Force the current-main comparison locally:

```bash
cd "$WT/pr-$PR"
BASE="$(git rev-parse origin/main)"
EV="$(mktemp)"; printf '{"pull_request":{"base":{"sha":"%s"},"body":""}}\n' "$BASE" > "$EV"
GITHUB_EVENT_NAME=pull_request GITHUB_EVENT_PATH="$EV" node tools/content_process.js
rm -f "$EV"
```

With no event vars the tool runs in `ledger` mode and will **not** surface this
class of failure. A PR that passes step 2 but fails here is `NEEDS_REBASE`.

### 5. Cross-reference the recorded CI verdict

```bash
gh pr checks <PR#>
gh pr view <PR#> --json statusCheckRollup
```

Any disagreement between the recorded verdict and steps 2–4 is a finding, not
noise: name the cause (stale base / gate step added since / environment).

### 6. Classify each failure

| Failing tool | Meaning | Remediation |
|---|---|---|
| `validate_refs.py` | reference manifest hash/licence/identity mismatch | re-register or fix the reference entry |
| `ko_terminology.js` | new/modified `{en, ko}` pair contradicts the catalog | correct the translation; legacy pairs warn, new ones fail |
| `ko_coverage.js --check` | new/modified pair lacks a `stage=reviewed` coverage record | see the stage contract below |
| `content_process.js` | `data/*.js` change does not match a `reserved` record merged to main, or base is stale | re-anchor the claim, or rebase (step 4) |
| `coverage.py --check` | reference fingerprint mismatch | refresh the override / re-register |
| `a2a_quorum.js --ci` | quorum head list or bundle target count moved | rebase and refresh the bundle |
| `similarity.py --gate` | recreated text overlaps the source | rewrite the overlapping passage |
| `commercial_readiness.js` | rights classification unclear | classify the asset |

**`ko_coverage` stage contract — read before "just marking it reviewed".**
Verified in `tools/ko_coverage.js`: a new or modified pair needs
`stage=reviewed` with a `review` object carrying `reviewer`, `reviewed_at`
(`YYYY-MM-DD`), a non-empty `note`, and validated evidence locators;
`stage=screen` must not carry `review`.

The code enforces only *presence and shape* — it does **not** check who the
reviewer was. Author exclusion (the reviewed record reflecting an independent
terminology review rather than the PR author's own assertion) is a **process
convention, not a gate**. Treat it as a review obligation precisely because
nothing will catch a violation for you.

### 7. Emit a verdict per PR

```
## PR #<N> — <title>
- head=<sha>  base=<ref>  mergeable=<…>  mergeStateStatus=<…>
- Verdict: READY | NEEDS_FIX | NEEDS_REBASE
- head-time gate:      <step>: PASS|FAIL …
- current-main gate:   <step>: PASS|FAIL …
- stale-base replay (step 4): PASS|FAIL
- BLOCK: <tool> — <symptom> → <exact remediation command>
- INFO:  <observation> (env gap, pre-existing failure, …)
```

Order the batch: `NEEDS_REBASE` first (it moves main-relative state), then
`NEEDS_FIX` in parallel, then `READY`.

### 8. Preserve evidence, then clean up

Tee every run to a log; keep logs **outside** the worktree so cleanup cannot
destroy them.

```bash
EV_DIR="${TMPDIR:-/tmp}/nclex-preflight-evidence/pr-$PR"
mkdir -p "$EV_DIR"          # …| tee "$EV_DIR/head-time.log" etc.
git -C "$REPO" worktree remove --force "$WT/pr-$PR"
git -C "$REPO" worktree prune
```

`--force` is required: a merge/abort cycle can leave the worktree dirty.
Only remove after the logs are written.

## Verification

- Every PR has **both** a head-time and a current-main result, plus the step-4
  replay. A missing baseline means the question was not answered.
- Each failure is attributed to a named tool with a concrete next command.
- Recorded-CI vs local disagreements are explained, not ignored.
- Evidence logs exist outside removed worktrees; no worktrees leak
  (`git worktree list`).
- Head-time PASS is an **observation, not a pass criterion** — a PR can fail at
  its own head (rules added since it branched) and that is a normal finding.

## Safety

- Detached worktrees only; never validate in the primary working tree.
- Local merges are `--no-commit --no-ff` and always `--merge --abort`ed; nothing
  is committed or pushed.
- GitHub access is read-only (`gh pr view` / `gh pr checks`). No comments, no
  merges, no dispatch.
- Never run `tools/a2a_dispatch.js` — it publishes.
- Record local Node/Python versions next to any local-only failure.

## Next steps

- `NEEDS_REBASE` → author rebases onto current main, then re-run this skill.
- `NEEDS_FIX` → hand back the exact failing tool and command.
- `READY` → proceed to `nclex-a2a-content-pipeline` for lane dispatch.
