# ccc-node

**CCC = Claude Code Cli** — source-only node harness for turning a Seoyoon/Hermes host into a Claude Code node: Claude settings, hooks, memory bootstrap, A2A worker helpers, diagnostics, and the Telegram bridge.

> Captured from `nosuk` and `soonwook` rollouts. This repo holds the mechanism, not live node secrets or memories.
>
> **Bridge provenance:** `bridge/` was vendored from a fork of [`terranc/claude-telegram-bot-bridge`](https://github.com/terranc/claude-telegram-bot-bridge), then intentionally developed here as part of `ccc-node`.

## Quickstart — new node path

```bash
git clone https://github.com/jinwon-int/ccc-node.git
cd ccc-node
./setup.sh --dry-run   # preview resolved paths and planned writes
./setup.sh             # install harness files/templates into ~/.claude and ~/.hermes

# Optional Telegram bridge:
cd bridge
./start.sh --path /root -d
```

After setup:

1. Fill placeholders in `~/.claude/CLAUDE.md` and template config files.
2. Provide node-local credentials through their normal tools (`claude` login, `gh auth login`, `~/.hermes/honcho.json`, wiki-agent install). Do not commit secrets.
3. Verify: `scripts/ccc-doctor.sh`, `scripts/validate-harness.sh`, and—if using the bridge—`bridge/start.sh --path /root --status`.
4. Start a fresh Claude Code session and confirm memory injection/status line behavior.

## What you get

| Area | One-line summary | Details |
|---|---|---|
| Memory hooks | SessionStart/PostCompact memory snapshot + background refresh; startup is no-network/fail-open. | [`docs/memory.md`](docs/memory.md) |
| Telegram bridge | Telegram ↔ Claude Code bridge with daemon/supervisor, streaming UI, push notifier, voice/media helpers. | [`bridge/README.md`](bridge/README.md), [`docs/bridge-ops.md`](docs/bridge-ops.md) |
| Harness settings | Claude settings, status line, Korean output style, plugin/standalone hook modes. | [`docs/harness.md`](docs/harness.md) |
| Doctor diagnostics | Read-only drift report plus conservative dry-run/apply repairs for settings and allowlisted files. | [`docs/doctor.md`](docs/doctor.md) |
| Security audit | Metadata-only permission/config/redaction checks; no matched secret text printed. | [`docs/security-audit.md`](docs/security-audit.md) |
| Agent-cron | Durable local task definitions, due/lock/run primitives, explicit scheduler execution. | [`docs/agent-cron.md`](docs/agent-cron.md) |
| A2A worker lane | Claude Code A2A poller/analysis-backend wiring, native Termux worker preflight. | [`docs/a2a-claude-worker.md`](docs/a2a-claude-worker.md) |
| Termux parity | Android/Termux constraints and VPS parity notes. | [`docs/android-termux-claude.md`](docs/android-termux-claude.md), [`docs/termux-vps-parity.md`](docs/termux-vps-parity.md) |
| Release/version anchor | `scripts/ccc-version.sh` exposes the harness tag/SHA anchor for doctor/fleet reports. | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## Repository layout

```text
claude/                    Claude Code harness templates, hooks, agents, skills, commands
bridge/                    Telegram bridge Python package and startup/setup scripts
docs/                      Living operator docs
docs/archive/              Historical closeout/roadmap records
hermes/                    Hermes-side templates; no real memory/secrets
scripts/                   Validation, diagnostics, memory, A2A, doctor, version helpers
schemas/                   JSON schemas for local task/state files
setup.sh                   Idempotent bootstrap; refuses to overwrite real node state
```

## Node profiles and path overrides

`setup.sh` defaults to `$HOME/.claude` and `$HOME/.hermes`; non-root installs can override:

| Variable | Default | Purpose |
|---|---|---|
| `CCC_CLAUDE_DIR` | `$HOME/.claude` | Claude Code harness target |
| `CCC_HERMES_DIR` | `$HOME/.hermes` | Hermes-side config templates |
| `CCC_WIKI_AGENT_BIN` | `$HOME/.wiki-agent/bin/wiki-agent` | Family Wiki reader/writer binary path |
| `CCC_BRIDGE_DEFAULT_PATH` | `$HOME` | Suggested Telegram bridge workspace |
| `CCC_STATE_DIR` | `$CCC_CLAUDE_DIR/state` | Local node state and memory index |
| `CCC_MEMORY_PROFILE` | `honcho` | Memory profile: `honcho`, `hybrid`, or `max-perf` |
| `CCC_MEMORY_CACHE_DIR` | `$CCC_CLAUDE_DIR/hooks/cache` | Wiki/Honcho cache metadata |

More memory-specific variables live in [`docs/memory.md`](docs/memory.md).

## Secret-handling policy

Never store raw secrets in this repository. Node-local credentials stay outside git:

| Item | Source |
|---|---|
| Claude OAuth | `claude` login creates `~/.claude/.credentials.json` |
| GitHub token | `gh auth login` node-local config |
| Honcho endpoint | `~/.hermes/honcho.json` |
| Real `MEMORY.md` / `USER.md` | node-local files derived from templates |
| Telegram bot token | bridge `.env` only |

`.gitignore` blocks credentials, real memory, caches, sessions, and token/secret-like files. Public visibility, release/tag publish, deployment/restart, DB mutation, provider/Telegram sends, credential movement, and history rewrites are separate approval gates.

## Contributing / support

- Local checks: `bash scripts/validate-harness.sh`, `ruff check .`, `mypy`, and `cd bridge && python -m pytest -q`.
- Contribution and release policy: [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Historical roadmaps/closeouts are under [`docs/archive/`](docs/archive/); living operational docs stay at the top of `docs/`.
