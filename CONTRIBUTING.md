# Contributing

Contributions should be small, reviewable, and safe to discuss publicly.

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
