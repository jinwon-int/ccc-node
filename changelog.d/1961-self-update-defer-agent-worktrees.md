- **Self-update: defer while the managed checkout has linked or agent
  worktrees; CONTRIBUTING forbids worktrees in the live checkout (#1961).**
  On a fleet node (2026-09-24) four Claude Code `Agent(isolation: worktree)` trees
  under `<checkout>/.claude/worktrees/` vanished right after a self-update tick
  fast-forwarded the live checkout; the deleting actor is still unconfirmed,
  and the dirty-tree guard did not stop that tick (suspected: an exclude rule
  hid the directory from `git status`). `ccc-self-update.sh run` now defers
  with exit `8` — before wrong-branch recovery, fetch, merge, setup or
  restart — while `git worktree list --porcelain` shows any linked worktree
  (a `prunable` entry whose directory is gone is ignored) or `.claude/worktrees`
  inside the checkout is non-empty (filesystem check, so ignore/exclude rules
  cannot hide it). Unlike the bridge-busy defer it is not capped and `--force`
  does not bypass it; each deferring tick logs `deferred reason=agent-worktrees`
  with the paths and notifies the owner (`SelfUpdate:deferred-worktrees`) with
  the offending paths. CONTRIBUTING.md now says to create agent/dev worktrees
  outside the managed checkout, based on a separate clone. Nodes that keep a
  linked worktree of the managed checkout stop updating until it is removed.
