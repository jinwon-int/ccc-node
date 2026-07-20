# GitHub transport policy

ccc-node uses the node-local authenticated `git` and `gh` CLI as the default
transport for every GitHub read and write. The OpenAI-curated GitHub plugin is
connector-first, so leaving it enabled causes an avoidable connector attempt
before the agent falls back to `gh` on nodes where the connector cannot finish
the operation.

## Installed policy

`setup.sh` applies this supported Codex plugin toggle under `CODEX_HOME`
(default `$HOME/.codex`):

```toml
[plugins."github@openai-curated-remote"]
enabled = false
```

The updater changes only that canonical table, validates TOML before and after,
writes atomically with mode `0600`, and never prints the config body. It rejects
symlinks, hardlinks, invalid TOML, and noncanonical inline/dotted forms instead
of risking unrelated node-local settings or credentials.

Codex's auto-managed global `AGENTS.md` block and Claude Code's `gh-pr-flow`
skill carry the matching behavioral policy:

- use local `git` and authenticated `gh` for GitHub operations;
- do not use a GitHub App, connector, MCP, or plugin unless the user explicitly
  requests that transport in the current task;
- report a failed `gh` operation instead of automatically retrying through a
  connector;
- verify `gh auth status` before the first authenticated operation when the
  session has not already done so.

## Verification

```bash
python3 scripts/ccc_codex_github_policy.py status --json
gh auth status
```

The policy status must be `disabled`, and `gh auth status` must show the
intended node identity. Plugin cache files may remain on disk; the config toggle,
not cache deletion, controls whether Codex loads the plugin.

## Explicit connector exception

If a current task explicitly requires the GitHub connector, open Codex, run
`/plugins`, select the installed GitHub plugin, and press Space to enable it.
Start a new session and state the connector requirement in that task. The next
ccc-node setup/self-update disables it again, restoring the fleet default.
