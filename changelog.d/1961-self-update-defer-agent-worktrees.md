- **Self-update: defer while worktrees live INSIDE the managed checkout;
  CONTRIBUTING forbids creating them there (#1961).** On gwakga (2026-09-24)
  four Claude Code `Agent(isolation: worktree)` trees under
  `<checkout>/.claude/worktrees/` vanished right after a self-update tick
  fast-forwarded the live checkout; the deleting actor is still unconfirmed,
  and the dirty-tree guard did not stop that tick (suspected: an exclude rule
  hid the directory from `git status`). `ccc-self-update.sh run` now defers
  with exit `8` — before wrong-branch recovery, fetch, merge, setup or
  restart — while a linked worktree's real path lies inside the checkout,
  `<checkout>/.claude/worktrees` is non-empty (filesystem check, so
  ignore/exclude rules cannot hide it), or `git worktree list` fails. Unlike
  the bridge-busy defer it is not capped and `--force` does not bypass it;
  each deferring tick logs `deferred reason=in-checkout-worktrees` with the
  paths and notifies the owner (`SelfUpdate:deferred-worktrees`). Linked
  worktrees OUTSIDE the checkout — present on 11 of 12 nodes in the
  2026-09-29 survey (`~/dev/<slug>`, daegyo's Matrix runtime source) — never
  defer or notify; the run logs only `worktree-gate ok external-worktrees=N`.
  CONTRIBUTING.md keeps the external `~/dev/<slug>` worktree recipe and adds
  the rule: never create worktrees (including agent `.claude/worktrees`)
  inside the managed checkout.
