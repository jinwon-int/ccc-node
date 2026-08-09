# Contributing

Contributions should be small, reviewable, and safe to discuss publicly.

## Where to work: never in a node's managed checkout

A fleet node keeps a checkout that `ccc-self-update.sh` updates on a schedule —
the path recorded in `~/.claude/self-update.repo`, typically `/opt/ccc-node`,
`$HOME/ccc-node`, or `/root/ccc-node`. **Keep it on `main`. Do not develop in
it.**

The updater is fail-closed: it refuses to run unless the checkout is on `main`
with a clean tree. So the moment you branch or leave an edit there, that node
stops updating — not until the next tick, but until a human puts it back. Before
the alerting added in #1060 nothing announced this, and a node sat 23h behind
`main` because a feature branch was left checked out (#1039).

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
- Reverting a stray branch is itself a repo mutation. Check the bridge's idle
  gate (`~/.telegram_bot/health.json`, `workload.active_requests`) first — the
  updater defers while the bridge is busy precisely because swapping the tree
  under a running session destroys in-flight work, and a manual `git checkout`
  bypasses that protection.

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
