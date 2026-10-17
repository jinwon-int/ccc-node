- **fix(termux): `termux-prepare-generation.sh` prunes old prepared generations (#2362).**
  Every self-update tick that moved `main` built a new ~1 GB
  `~/.ccc-node/preparations/main-<sha7>-<date>` generation and nothing ever
  removed one (gongyung 23 generations / 12 GB, daegyo 15 / 6.2 GB). After a
  successful repoint, or on the already-serving path — never after a failed
  build — the script now removes old script-built `main-*` generations
  (`git worktree remove --force`, `rm -rf`, `git worktree prune`). Always
  kept: the serving generation, the targets of the newest
  `CCC_TERMUX_GENERATIONS_KEEP` (default 2, minimum 1)
  `bridge-current.prev-*` records (the operator's rollback targets), any
  generation referenced by a live process (cmdline/exe/cwd), anything changed
  in the last 24 h (an in-progress build or a fresh failure kept for
  diagnosis), and every hand-made non-`main-*` generation. Fail-closed when
  the serving generation cannot be resolved. `--dry-run` prints
  `would prune <gen>`; `CCC_TERMUX_PREPARE_PRUNE=0` disables it.
  `termux-prepare-generation.test.sh` 42/42 (12 new; 5 fail on the old script).
