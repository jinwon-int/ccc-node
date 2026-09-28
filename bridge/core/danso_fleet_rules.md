<!-- ccc-fleet-rules:v1 managed=ccc-node source=bridge/core/danso_fleet_rules.md — installed by the bridge at every start; edit the repo file, not this copy -->
# Fleet operating rules (loaded at every Danso session start)

## Merging a PR: self-serve the cross-account approval, bounded waiting

- Merge through the merge skill: `gh-pr-flow-danso` in available_skills when present, otherwise
  `/opt/ccc-node/skills/gh-pr-flow/SKILL.md`. These rules are policy, not an approval.
- **Approval policy (owner, 2026-09-28):** if the user asked you to merge in the current task, a PR
  **you opened in this task** may be cross-account approved and merged **without asking again**, only
  when: head unchanged since you reviewed the diff, every required check on it green, `MERGEABLE`
  (then `CLEAN` before merging). Any other PR, or no merge instruction in this task: ask the user
  once with numbered options and end the turn — do not poll.
- Reviewer = the account that did NOT author the PR (`jinon86` ↔ `seoseo-ai`). Approve through the
  fail-closed relay helper (credentials stay on the relay node):
  `CCC_EXPLICIT_USER_APPROVAL=1 bash /opt/ccc-node/codex/skills/gh-pr-flow/scripts/approve-via-relay.sh --review-profile <reviewer> --repo jinwon-int/<repo> --pr <n> --expected-head <sha> --operator-approved --ssh-target seoseo`
  Then `gh pr merge <n> --squash --delete-branch`, or `gh pr merge <n>` to enqueue on a merge-queue repo.
- **Waiting limits:** head CI up to 30 minutes, merge queue up to 30 minutes after enqueue; stop early on
  a failed check or eviction; at a cap report "enqueued/pending" with PR number and exact head, then end
  the turn. A helper refusal for pending checks or `UNKNOWN` mergeability means wait within the cap;
  any other refusal (actor/author mismatch, head changed, failed check, no eligible reviewer) means stop
  and report. Never wait for a human's or another session's approval. No sleep loops.
- Never `--admin`, never weaken branch protection, never print, copy or export a token, never push to
  a PR authored by the other account.
