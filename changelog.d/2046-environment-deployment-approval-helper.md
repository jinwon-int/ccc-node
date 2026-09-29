- **gh-pr-flow: fail-closed relay helper for GitHub Actions environment
  deployment approval (#2046).** `codex/skills/gh-pr-flow/scripts/approve-deployment-via-relay.sh`
  approves one pending environment (e.g. a `release` environment whose required
  reviewer is `jinon86`) through the relay-held `jinon86` profile, mirroring
  `approve-via-relay.sh`: fresh explicit approval (`CCC_EXPLICIT_USER_APPROVAL=1`
  + `--operator-approved`), inputs validated before SSH, root-owned credential
  boundary, exact run identity (repository/not a fork, head SHA, branch, event,
  workflow path), exactly one pending environment approvable by the actor,
  review-history verification after the POST, and idempotent re-runs
  (`already_approved`; exit `66` for a run no longer waiting that was never
  approved). Previously the only path was an ad-hoc `gh api` call on the relay
  node, outside every fail-closed helper. Both gh-pr-flow SKILL.md files document
  it as a release-gate action needing fresh approval per run.
