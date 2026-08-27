---
name: infra-gate-admin-merge-discipline
description: Land a change on a path that a required CI gate denies on every branch (scripts/, .github/, workflow or lint tooling) using the repo's sanctioned admin escape hatch, without silently losing the verification the gate would have run. Use when a PR check reports a denied infrastructure path, when a gate fails before reaching its test steps, when deciding whether --admin is legitimate here, or when a repo documents enforce_admins=false as the intended path for infra changes. Not for ordinary code or content PRs — those go through gh-pr-flow, which forbids --admin.
---

# infra-gate-admin-merge-discipline

Some repos deny infrastructure paths (`scripts/`, `.github/`, keys) at a
**required** check on *every* branch, and deliberately leave `enforce_admins`
false so an admin can merge past the red check. The denial is not a bug to route
around — it is the mechanism that forces the change to be deliberate, visible in
the checks UI, and attributable in the audit log.

**This is the narrow exception to `gh-pr-flow`'s rule "never use `--admin` merely
to bypass branch protection."** That rule is right for code and content. It does
not cover the case where the repo itself documents `--admin` as the only way in.
Confirm you are in that case before using this skill — read the gate's own
comments and find the sentence sanctioning the bypass. If no such sentence
exists, you are bypassing a protection, not using an escape hatch: stop.

## The failure this prevents

A path-deny gate usually rejects at its **first** step and exits. Every later
step in that job — compiling, linting, running the test suite — never executes.
The PR shows one red check, you merge past it as authorized, and the tests you
carefully wired into CI have **never run anywhere**. Nothing warns you; the red
X looks the same either way.

## Procedure

1. **Get fresh owner approval.** Infrastructure changes are release-shaped. Prior
   approval for the feature is not approval to merge past a required check. Say
   plainly that this needs an admin bypass and why.

2. **Confirm you actually hold admin, and confirm the hatch exists.**
   ```bash
   gh api repos/<owner>/<repo> --jq '.permissions'
   ```
   Then read the gate source for the sentence that sanctions the bypass. If the
   gate denies the path but says nothing about an admin path, escalate to the
   owner instead of merging.

3. **Read the job log and prove the failure is path denial alone.**
   Never judge from the red X. A test failure and a path denial look identical
   at the PR level.
   ```bash
   gh pr checks <pr> --repo <owner>/<repo>
   gh run view <run-id> --repo <owner>/<repo> --log-failed | tail -40
   ```
   You want the denial line and nothing else failing. If any test or lint step
   also failed, this skill does not apply — fix the code first.

4. **Reproduce locally every step the gate skipped.** Open the workflow, find the
   steps after the deny point, and run those exact commands:
   ```bash
   sed -n '/name: <the job>/,$p' .github/workflows/<gate>.yml
   ```
   Run them verbatim — same flags, same order. This is the verification you are
   about to merge without. Record the output; it is the only evidence that
   exists for this merge.

5. **Merge, deliberately.**
   ```bash
   gh pr merge <pr> --repo <owner>/<repo> --squash --admin --delete-branch
   ```

6. **Verify the gate still works — especially if you edited the gate.**
   A broken gate fails open for everyone and the next PR is the one that finds
   out. Re-run the gate's own tests from the merged default branch:
   ```bash
   git fetch origin <default> && git checkout origin/<default>
   <the gate's self-tests>            # e.g. scripts/test-*-gate-*.sh
   ```
   Also re-run the checks you added, from the merged branch rather than your
   working copy.

7. **Record it.** Note in the PR body and the durable log: the approval, that the
   admin hatch was used, the confirmed denial reason, and that the skipped steps
   were reproduced locally. An audit entry saying only "merged past red check" is
   indistinguishable from an abuse of the hatch.

## Safety

- **Red check ≠ one reason.** Always read the log. Merging past a gate you have
  not diagnosed is the abuse the gate exists to prevent.
- **Check status and mergeability are independent.** A PR can show every check
  green and still be unmergeable (`mergeable_state: dirty`), and the reverse.
  Query both:
  ```bash
  gh api repos/<owner>/<repo>/pulls/<pr> --jq '{mergeable, mergeable_state}'
  ```
- **Never widen the gate to make your change fit.** Editing the deny list so the
  PR goes green is strictly worse than using the hatch: it removes the control
  for everyone, permanently, and leaves no audit trace.
- If the same path needs the hatch repeatedly, that is a signal to propose a
  reviewed lane for it — not to normalize the bypass.

## Verification

- The job log shows the denial and no other failing step.
- Every skipped step was run locally with its output recorded.
- The gate's own self-tests pass from the merged default branch.
- The PR body states the approval, the reason, and the local reproduction.
