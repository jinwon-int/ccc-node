# Contributing

Contributions should be small, reviewable, and safe to discuss publicly.

## Where to work: never in a node's managed checkout

A fleet node keeps a checkout that `ccc-self-update.sh` updates on a schedule —
the path recorded in `~/.claude/self-update.repo`, typically `/opt/ccc-node`,
`$HOME/ccc-node`, or `/root/ccc-node`. **Keep it on `main`. Do not develop in
it.**

The updater is fail-closed: it refuses to run unless the checkout is on `main`
with a clean tree. So the moment you branch or leave an edit there, that node
stops updating — not until the next tick, but until a human puts it back
(#1039). Two mechanical guards back this up (#1328): setup.sh installs a
post-checkout hook into the managed checkout that warns AT THE MOMENT you
switch off `main`, and the updater itself now auto-recovers a wrong-branch
stall when it is provably lossless (clean tree and the stray branch fully
pushed — every commit already on the remote). Unpushed commits, dirty trees,
or a `main` held by a linked worktree still require the human path below.

Develop in a separate worktree instead:

```bash
git -C /opt/ccc-node worktree add ~/dev/<slug> -b <type>/<slug> origin/main
```

The managed checkout stays on `main` and keeps updating; git also refuses to
check out the same branch twice, which enforces part of this for you. Two
things a worktree does **not** solve:

- `setup.sh` installs from its own location, so running it from a dev worktree
  installs unmerged code as the node's harness. Run it only from the managed
  checkout.
- Reverting a stray branch is itself a repo mutation. The updater's
  auto-recovery (#1328) only fires on the provably lossless shape and only
  after the bridge's idle gate; a MANUAL `git checkout` bypasses that
  protection. Check the bridge's idle gate
  (`~/.telegram_bot/health.json`, `workload.active_requests`) first — the
  updater defers while the bridge is busy precisely because swapping the tree
  under a running session destroys in-flight work.

## Claim an issue before you build it

Multiple workers — human and agent nodes alike — pull from the same issue
backlog, and nothing else coordinates who implements what. On 2026-08-18 the
same #1081 piece was independently implemented twice and the PRs opened **46
seconds apart** (#1141, #1142); the second implementation, hours of work with
green CI, was closed unmerged (#1143 records the measurements). A design
comment on an issue is not a reservation: both implementations started from
the same design comment, each reading it as "ready for anyone."

So make the reservation explicit before you start implementing:

1. **Claim first.** Self-assign the issue (preferred), or leave a start
   comment stating the scope you are taking and roughly when. Do this before
   branching, not when opening the PR — the PR is hours too late.
2. **Respect existing claims.** If an issue has an assignee or a live start
   comment, do not begin a competing implementation. Reviewing, commenting,
   and designing stay open to everyone.
3. **Release what you drop.** Un-assign or comment when you stop. A claim
   with no linked branch or PR after 7 days can be treated as released.

This covers repository backlog work only. Lane work dispatched through the
A2A broker already carries its own reservation (task claims), and does not
need a second one here.

## Operator decisions and review scope

Explicit operator-approved behavior and acceptance criteria are requirements,
not suggestions for a cleanup or security-review pass. Reviewers may harden the
implementation while preserving those semantics, but must not invert defaults,
opt-in/opt-out direction, or the approved operating model without a new,
explicit operator decision. If a security concern appears to require such a
policy change, stop and present the conflict instead of silently redesigning the
change. Authority to tidy, review, approve, or merge a PR does not by itself
authorize a product-policy reversal.

Before opening a pull request:

1. Keep runtime credentials, local state, generated artifacts, private paths,
   and raw logs out of the diff.
2. Add or update tests when behavior changes.
3. Run the repository's documented checks where practical.
4. State whether the change is source-only.

Useful local checks:

```bash
bash scripts/validate-harness.sh
ruff check .
mypy
cd bridge && python -m pytest -q
```

The following actions remain separate approval gates and must not be bundled
into ordinary contribution PRs: visibility changes, release/tag/package publish,
production deploy/restart/reload, database mutation, provider/Telegram live
sends, credential movement, force-push/history rewrite, or other destructive
operations.

## Release policy

- Version tags use `v0.MINOR.PATCH` until the harness reaches a stable 1.0
  contract. Use MINOR for user-visible features/behavior changes and PATCH for
  fixes, docs, and tooling-only bundles.
- Cut releases in trains, not on every merge. Prefer tagging after a meaningful
  issue bundle lands, with a practical upper bound of one release train per week.
- Before tagging, move completed notes from `CHANGELOG.md` `Unreleased` into a
  dated version section, run the local checks above, and verify
  `scripts/ccc-version.sh` resolves the intended tag after `git fetch --tags`.
- Creating/pushing tags and GitHub Releases is a separate release approval gate;
  do not do it as part of a normal PR without explicit operator approval.

<!-- ruleset attribution check 2026-08-31 -->
