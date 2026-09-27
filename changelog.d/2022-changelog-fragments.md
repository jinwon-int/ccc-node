- **Changelog entries are fragment files now (#2022).** Every PR used to
  prepend to `CHANGELOG.md` / `bridge/CHANGELOG.md`, so two open PRs always
  conflicted on the same line — on the merge queue each conflict cost a new
  head, a CI rerun, a fresh exact-head approval and a re-enqueue (three times
  on 2026-09-27 alone). A PR now adds `changelog.d/<issue>-<slug>.md` or
  `bridge/changelog.d/<issue>-<slug>.md`; `scripts/changelog_fragments.py`
  validates them in CI (`check`), shows the result (`preview`) and folds them
  into the changelogs at release (`apply`). The release workflow refuses a tag
  while fragments are pending.
