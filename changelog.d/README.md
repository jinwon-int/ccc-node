# Changelog fragments → `CHANGELOG.md`

Add **one new file per change** here instead of editing `CHANGELOG.md`
directly (#2022). Two PRs that both prepend to the changelog always conflict,
and on the merge queue every conflict costs a new head, a CI rerun and a
fresh exact-head approval.

- **Name:** `<issue-number>-<slug>.md`, e.g. `2022-changelog-fragments.md`
  (lowercase slug; digits, letters, `.`, `_`, `-`).
- **Content:** the entry exactly as it would appear in the changelog — one or
  more `- ` bullets, no headings.
- **Release:** `python3 scripts/changelog_fragments.py apply` inserts every
  fragment under `## [Unreleased]` (highest issue number first) and deletes the files; the
  release workflow refuses a tag while fragments are pending.
- `python3 scripts/changelog_fragments.py check` (CI) validates them;
  `preview` prints what `apply` would insert.
