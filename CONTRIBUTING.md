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
(#1039). Three mechanical guards back this up (#1328, #1397): setup.sh
installs the managed-checkout guard as a post-checkout hook that warns AT THE
MOMENT you switch off `main` and as a pre-commit hook that REFUSES commits
while off `main` (`CCC_MANAGED_CHECKOUT_GUARD=warn` makes it advisory, `=0`
silences both); and the updater auto-recovers a wrong-branch stall whenever
the tree is clean — a fully pushed stray branch switches straight back, an
unpushed one is pinned under `refs/ccc-stray/<branch>/<utc-ts>` first so no
commit can be orphaned, and the notice tells you where it lives. A dirty tree
or a `main` held by a linked worktree still require the human path below.

### Never create worktrees inside (or off) the managed checkout

**Never create an agent or dev worktree inside the live managed checkout** —
in particular not Claude Code's default agent location
`<checkout>/.claude/worktrees/<name>` (what `Agent(isolation: worktree)` uses
when the session's repository IS the managed checkout). On a fleet node
(2026-09-24, #1961) four such agent worktrees vanished right after a
self-update tick fast-forwarded the checkout; the deleting actor is still
unconfirmed, and the dirty-tree guard did not stop that tick (suspected: the
directory was hidden from `git status` by an exclude rule). Put worktrees on a
path outside the checkout instead, and base them on a **separate clone**, not
on the managed checkout itself:

```bash
git clone https://github.com/jinwon-int/ccc-node ~/work/ccc-node   # once
git -C ~/work/ccc-node worktree add ~/work/wt/<slug> -b <type>/<slug> origin/main
```

Run agent sessions that need `isolation: worktree` from that clone, so their
`.claude/worktrees/` lands under `~/work/ccc-node`, never under the managed
checkout.

The updater enforces this (#1961): while the managed checkout has **any
linked worktree** (`git worktree list` shows more than the main entry,
wherever the linked tree lives) or a **non-empty `.claude/worktrees/`**
(checked on the filesystem, so ignore/exclude rules cannot hide it), every
tick **defers** with exit 8 — nothing is fetched, merged, installed or
restarted, `--force` does not override it, and the owner is notified with the
offending paths. The node stops updating until the worktree is removed, which
is why a linked worktree of the managed checkout (even one outside it, the
previously documented `git -C /opt/ccc-node worktree add ~/dev/<slug>`
recipe) is no longer the recommended dev path. See
[docs/self-update.md](docs/self-update.md#worktree-gate-1961).

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
  (`~/.telegram_bot/health.json`, `workload.turn_occupancy.state` /
  `workload.active_requests`; see
  [docs/bridge-ops.md](docs/bridge-ops.md#before-a-manual-restart-occupancy-check))
  first — the updater defers while the bridge is busy precisely because
  swapping the tree under a running session destroys in-flight work.

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

`validate-harness.sh` runs every tracked `*.test.sh` and takes **well over ten
minutes** on a single machine — it streams `ok …` lines as it goes, so it is
working, not hung. To get a faster signal, run one phase or one shard (CI runs
the static phase plus four shards in parallel; see
[`docs/ci-governance.md`](docs/ci-governance.md)):

```bash
CCC_HARNESS_PHASE=static bash scripts/validate-harness.sh    # fast, no suites
CCC_HARNESS_PHASE=hook-tests CCC_HARNESS_SHARD=1/4 bash scripts/validate-harness.sh
```

The bridge suite is likewise ~6 minutes; it is hermetic and needs no network.

The following actions remain separate approval gates and must not be bundled
into ordinary contribution PRs: visibility changes, release/tag/package publish,
production deploy/restart/reload, database mutation, provider/Telegram live
sends, credential movement, force-push/history rewrite, or other destructive
operations.

## Changelog entries

Do **not** edit `CHANGELOG.md` or `bridge/CHANGELOG.md` in a feature PR. Add
one new fragment file instead (#2022):

- harness changes: `changelog.d/<issue-number>-<slug>.md`
- bridge changes: `bridge/changelog.d/<issue-number>-<slug>.md`

The file holds the entry exactly as it would appear in the changelog — one or
more `- ` bullets, no headings. Separate files never conflict, so open PRs no
longer collide on the changelog's first line (each collision on the merge
queue cost a new head, a CI rerun and a fresh exact-head approval). CI runs
`scripts/changelog_fragments.py check`; `preview` shows the assembled result.

## Release policy

- Version tags use `v0.MINOR.PATCH` until the harness reaches a stable 1.0
  contract. Use MINOR for user-visible features/behavior changes and PATCH for
  fixes, docs, and tooling-only bundles.
- Cut releases in trains, not on every merge. Prefer tagging after a meaningful
  issue bundle lands, with a practical upper bound of one release train per week.
- Before tagging, fold pending changelog fragments in with
  `python3 scripts/changelog_fragments.py apply` in the release PR and tag
  that PR's merge commit (the release workflow refuses a tag while any
  `changelog.d/` fragment is pending; a fragment merged after the release PR
  would otherwise block the tag), then move completed notes from `CHANGELOG.md`
  `Unreleased` into a dated version section, run the local checks above, and verify
  `scripts/ccc-version.sh` resolves the intended tag after `git fetch --tags`.
- Creating/pushing tags and GitHub Releases is a separate release approval gate;
  do not do it as part of a normal PR without explicit operator approval.
