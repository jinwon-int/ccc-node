# Security Policy

## Current status

This repository contains the ccc-node Claude Code harness, Telegram bridge, hook
scripts, skills, and supporting automation. The repository is still an early
project: public source visibility or a GitHub release does not imply production
readiness, stable API guarantees, package publication, deployment approval, or
permission to exercise live provider, Telegram, database, or worker operations.

Operational actions such as production deployment, provider sends, database
mutation, credential movement, release publication, package/image publication,
history rewrite, and visibility changes remain separate approval-gated actions.
Normal issues, pull requests, local tests, and CI runs do not authorize those
actions.

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | Security fixes are accepted while the project is under active development. |
| GitHub Releases | Best-effort source snapshots only unless a release explicitly states support. |
| Older commits or private forks | Not supported. Reproduce issues on current `main` when possible. |

Because the project is pre-stable, maintainers may fix security issues on
`main` without backporting to older tags. If a future release establishes a
support window, this section should be updated with exact version ranges.

## Reporting a vulnerability

Do **not** open public GitHub issues, pull requests, discussions, screenshots, or
logs that contain vulnerability details, proof-of-concept exploit steps, secrets,
tokens, private URLs, personal data, private hostnames, internal IP addresses, or
raw runtime/session dumps.

Use GitHub private vulnerability reporting for this repository:

https://github.com/jinwon-int/ccc-node/security/advisories/new

If that private reporting route is unavailable, open only a non-sensitive
maintainer-contact issue that says you need a private security channel, or use an
already established private maintainer contact path. Share technical details only
after a maintainer confirms the private route.

When reporting, include as much safe, redacted context as possible:

- affected component, command, workflow, or file path
- expected vs. observed behavior
- minimal reproduction steps using placeholders instead of real credentials
- impact assessment and whether exploitation requires local access
- relevant commit SHA or release tag
- whether the issue affects live operations or only local/dev workflows

Use placeholders such as `<telegram-bot-token>`, `<github-token>`,
`<provider-api-key>`, `<private-host>`, and `<project-root>` instead of real
values.

## Response expectations

Maintainers aim to acknowledge private vulnerability reports within 3 business
days and provide an initial triage result within 10 business days when enough
information is available. Complex issues may need more time; maintainers should
keep the reporter updated on material status changes.

If the report is accepted, maintainers will coordinate a fix plan, disclosure
scope, credit preference, and any advisory publication timing in the private
security advisory thread. Public disclosure should wait until maintainers have a
reasonable opportunity to patch or mitigate the issue.

## Secret handling

- Do not commit real API keys, bot tokens, cookies, sessions, private keys,
  authorization headers, production logs, private host paths, or raw runtime
  data.
- Example files must use placeholders only.
- Runtime credentials belong in local environment variables or operator-owned
  secret stores outside this repository.
- If a secret is accidentally exposed, treat it as compromised: remove the
  exposure, rotate the secret through the owning system, and document only
  redacted evidence in public artifacts.

## Hard safety boundary

The following actions require explicit operator approval that names the exact
action, target, and rollback/no-op boundary:

- production deploys or process restarts
- live provider or Telegram sends
- production database mutation
- terminal outbox ACK/replay mutation
- release, tag, package, npm, Docker, image, or GHCR publication
- repository visibility, ownership, branch-protection, or ruleset changes
- secret or credential movement, rotation, or disclosure
- history rewrite or force push

Security PRs in this repository should be source-only unless the operator has
explicitly approved one of the actions above.
