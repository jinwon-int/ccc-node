# Version and provenance

ccc-node has one operational source of truth:
[`jinwon-int/ccc-node`](https://github.com/jinwon-int/ccc-node). The `bridge/`
directory was originally vendored from `terranc/claude-telegram-bot-bridge`, but
upstream releases are review signals only and never update a running ccc-node
checkout automatically. See [Bridge upstream and i18n policy](bridge-upstream-i18n-policy.md).

## Installed runtime identity

The installed identity is the output of:

```bash
scripts/ccc-version.sh
# or, from bridge/:
./start.sh --path /path/to/project --version
```

`ccc-version.sh` runs `git describe --tags --dirty --always` in the canonical
checkout. Its result binds a report to the actual installed commit and makes a
dirty worktree visible. `ccc-doctor` and the fleet matrix consume the same
value.

Do not use any of the following as an installed-update decision:

- `bridge/CHANGELOG.md` versions: historical bridge component history;
- `bridge/pyproject.toml` version: Python distribution metadata;
- top-level release tags alone: useful labels, but the checkout SHA/dirty state
  is still the runtime identity;
- upstream bridge releases: review inputs only.

These values may intentionally advance on different schedules. A release may
later unify them, but until then `scripts/ccc-version.sh` is the sole runtime
version anchor.

## Canonical update path

The only in-repository live updater is:

```bash
scripts/ccc-self-update.sh run
# compatibility entry point from bridge/:
./start.sh --path /path/to/project --upgrade
```

The bridge entry point delegates to `ccc-self-update.sh`; it does not query
GitHub release APIs, compare bridge changelog versions, or run an independent
`git pull`. Before delegation it requires `origin` to be an exact HTTPS or SSH
form of `github.com/jinwon-int/ccc-node` and pins the branch to `main`; a drifted
or credential-bearing remote is rejected without printing the remote value. The
canonical updater then:

1. resolves the configured ccc-node checkout and expected branch (`main` by
   default);
2. requires a clean worktree and exact expected branch;
3. fetches `origin/main` and applies only a fast-forward merge;
4. installs through the repository setup path with transactional artifact and
   repository rollback;
5. restarts only operator-allowlisted services and verifies them;
6. records the actual old/new commit IDs in its audit record.

A fork-only commit on `origin/main` is therefore detected even when no release
tag changed. An upstream-only release cannot trigger an update.

`ccc-self-update.sh` exit codes remain authoritative. In particular, exit `8`
means the update was deferred because the bridge is serving work; the bridge
compatibility entry point preserves that code and does not claim completion.

## Upstream sync boundary

There is no automatic upstream merge. Maintainers may review a specific upstream
security or compatibility change, port it through a normal ccc-node issue and
pull request, and validate it against ccc-node's safety boundaries. That review
must not change the configured origin, updater target, or runtime identity.
