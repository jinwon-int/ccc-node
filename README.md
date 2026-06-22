# ccc-node

**CCC = Claude Code Cli** — the unified node-setting monorepo for turning a Seoyoon/Hermes
node into a **Claude Code node (클코 노드)**: the same harness configuration proven on
`nosuk` (VPS2) and `soonwook` (VPS6), plus the Telegram bridge that connects Claude Code
to Telegram, with all secrets and node-local state stripped out and replaced by placeholders.

> Captured from the `nosuk` node setup on **2026-06-19** and updated after the `soonwook`
> VPS6 rollout on **2026-06-20** so other nodes can be bootstrapped the same way. This repo
> holds the **mechanism**, not any node's live secrets/memory.
>
> **Bridge provenance:** `bridge/` was vendored from a fork of
> [`terranc/claude-telegram-bot-bridge`](https://github.com/terranc/claude-telegram-bot-bridge)
> (commit history preserved). The upstream relationship is **intentionally dropped** — the
> bridge is now developed here, independently, as part of `ccc-node`.

## What this gives a node

- **SessionStart / PostCompact memory injection** — `hooks/load-memory.sh` injects a snapshot
  at session start: built-in `MEMORY.md`/`USER.md` + cached Family Wiki prefetch + cached
  Honcho working memory, then fires a detached background refresh for the next session.
- **Background cache refresh** — `hooks/refresh-memory.sh` updates the Wiki + Honcho caches
  out-of-band (single-flight via `flock`, fail-open) so startup never blocks on slow calls.
- **Harness settings** — `settings.json` (permissions + hook wiring) and `settings.local.json`.
- **CLAUDE.md template** — the operating-policy skeleton (Wiki-first, A2A/Nexus, GitHub
  hygiene, fresh-approval rules) with node/user identity as `<PLACEHOLDERS>`.
- **Telegram bridge** (`bridge/`) — a lightweight bot that bridges Claude Code to Telegram
  for any local folder, with autostart/supervisor support. Run via `bridge/start.sh`.
- **Push notifier** (`bridge/core/push_notifier.py`) — opt-in, owner-only delivery of
  Claude Code lifecycle notifications. The notify hook (`CCC_NOTIFY_TELEGRAM=1`) writes
  redacted summaries to a spool; the bridge sends them so the **bot token never leaves the
  bridge**. Disabled by default; enable via `CCC_PUSH_ENABLED` + an owner chat id.
- **Status line** (`hooks/statusline.sh`) — node · model · git · context % · `⚠200k` · cost ·
  A2A marker · output style, wired via `settings.json` `statusLine`. Set `CCC_NODE` (or
  `~/.claude/state/node.txt`) for the node label; falls back to the short hostname.
- **Output style** (`output-styles/ccc-report.md`) — Korean structured-reporting default
  (확정/변경/리스크/다음, 진행 내레이션, 번호형 질문), activated via `settings.json`
  `outputStyle`. Switch anytime with `/config` → Output style.
- **Doctor diagnostics** (`scripts/ccc-doctor.sh`, `/doctor`) — harness drift classification for
  settings, hook wiring, output style, status line, and bridge status. `--fix` is dry-run by
  default; `--fix --apply` defaults to scoped `settings.json` repair after a backup tar, while
  `--fix --apply --scope=files` reinstalls only allowlisted hook/output-style files after a
  scoped backup and refuses symlinks/path escapes/plugin double-firing risk. `--rollback` is
  dry-run by default, and `--rollback --apply` restores only `settings.json` from the latest
  doctor backup after creating a pre-rollback backup. Manual/risky/system-level items stay
  fail-closed.
- **Security audit** (`scripts/ccc-security-audit.sh`, `/security-audit`) — read-only,
  metadata-only checks for sensitive file permissions, settings allowlist posture, scanner
  integrity, and push-spool/cache redaction risk. It never prints matched secret text or file
  contents; `--fix` is reserved for a later repair slice.
- **Headless runner** (`headless.sh`) — `claude -p` wrapper for cron/A2A/CI with JSON output
  and a read-only tool baseline; guard enforcement still applies.
- **Agent-cron store/list/due** (`scripts/agent-cron.sh`, `/agent-cron`) — first-class durable
  task-definition surface for future headless cron work. The current slices validate/list
  `~/.claude/state/agent-cron/tasks.json` against `schemas/agent-cron-task-store.schema.json`
  and provide a read-only `due` resolver for schedule/catch-up planning. They do not execute
  tasks, update `lastRunAt`, send Telegram/provider messages, or install schedulers.
- **A2A Claude Code worker lane** — documentation for nodes whose broker poller keeps the
  historical `a2a-hermes-worker` service name while the task analysis adapter is switched to
  `claude-a2a-analysis-bridge` and broker metadata reports `runtime=claude-code`. The
  `claude/agents/a2a-*.md` roster carries advisory `model_tier` metadata (read-only
  explorer/researcher = `low-cost`, implementer/verifier = `upper`) and requires cost/token
  notes when runner accounting is available. See [`docs/a2a-claude-worker.md`](docs/a2a-claude-worker.md).

## Quick start

```bash
git clone https://github.com/jinwon-int/ccc-node.git
cd ccc-node
./setup.sh --dry-run   # preview
./setup.sh             # install into ~/.claude and seed ~/.hermes templates

# Telegram bridge (optional, run from the bridge/ subdir):
cd bridge && ./start.sh --path /root -d   # daemon-supervised start
```

Then complete the checklist `setup.sh` prints (fill placeholders, set `honcho.json`,
install `wiki-agent`, `gh auth login`, start a fresh session to verify injection).

### Install the portable surface as a plugin (optional)

The node-agnostic surface — enforcement guard + observability hooks, A2A agents, skills,
and slash commands — is also packaged as a Claude Code plugin. The marketplace catalog
lives at `.claude-plugin/marketplace.json` and points the plugin root at the existing
`claude/` tree (`source: "./claude"`), so components are auto-discovered with no duplication:

```bash
/plugin marketplace add jinwon-int/ccc-node
/plugin install ccc-node@ccc-node
```

The plugin and `setup.sh` are complementary: the **plugin** carries the portable surface,
while **`setup.sh`** installs the node-local memory bootstrap (SessionStart/PostCompact
injection, working-state checkpoint) that is inherently node-specific. See `CHANGELOG.md`.

**Avoiding double-firing.** The portable enforcement/observability hooks (guard/audit/redact/
notify) must have exactly one owner — `settings.json` and the plugin both register hooks and
Claude Code does not de-duplicate them. So `setup.sh` composes `settings.json` from two sources:
`claude/settings.base.json` (node-local hooks + statusLine + outputStyle, always installed) and
`claude/hooks/enforcement-overlay.json` (the portable hooks). Pick one mode per node:

- **Standalone** (default): `./setup.sh` merges base + overlay — `settings.json` owns everything,
  no plugin required.
- **Plugin mode**: `./setup.sh --with-plugin` installs lean settings (base only); the installed
  **plugin** owns the portable hooks. Use this on nodes that consume ccc-node via the marketplace.

Don't enable the plugin on a node installed standalone (or vice-versa) — that double-fires the
portable hooks. The validator asserts the overlay and the plugin's `hooks/hooks.json` stay
equivalent so the two modes enforce identically.

## Layout

```
claude/
  settings.base.json       # node-local hooks + statusLine + outputStyle (always installed)
  hooks/enforcement-overlay.json  # portable hooks; merged in for standalone, omitted in plugin mode
  settings.local.json      # local permission allowlist
  CLAUDE.md.template        # operating policy w/ <PLACEHOLDERS> -> ~/.claude/CLAUDE.md
  hooks/
    load-memory.sh         # snapshot injector (verbatim; paths only)
    refresh-memory.sh      # Wiki+Honcho cache refresh (endpoint read from honcho.json)
hermes/
  memories/MEMORY.template.md   # -> ~/.hermes/memories/MEMORY.md
  memories/USER.template.md     # -> ~/.hermes/memories/USER.md
  honcho.template.json          # -> ~/.hermes/honcho.json (set baseUrl/peer/target)
bridge/                    # Telegram <-> Claude Code bridge (vendored, history preserved)
  start.sh                 # daemon/supervisor entry (--path / --stop / --status / -d)
  core/ interaction/ ...   # bridge source (upstream-independent fork)
docs/
  a2a-claude-worker.md     # A2A poller-vs-analysis-backend wiring + verification
setup.sh                   # idempotent bootstrap (won't overwrite existing real files)
.gitignore                 # blocks credentials, live memory, caches, sessions
```

## Secret-handling policy (important)

This repo follows the Seoyoon rule: **never store raw secrets — only locations / handling rules.**
The following are intentionally absent and must be provided per node:

| Item | Where it comes from |
|---|---|
| `~/.claude/.credentials.json` (Claude OAuth) | created by `claude` login |
| GitHub token | `gh auth login` (node-local; never committed) |
| Honcho endpoint `baseUrl` | set in `~/.hermes/honcho.json` (gitignored) |
| Real `MEMORY.md` / `USER.md` content | written per node from the templates |

`.gitignore` blocks `.credentials.json`, `honcho.json`, real `MEMORY.md`/`USER.md`,
hook caches, sessions, and anything matching `*token*` / `*secret*`.

## Dependencies a node needs

- [`jinwon-int/wiki-agent`](https://github.com/jinwon-int/wiki-agent) installed at
  `/root/.wiki-agent/bin/wiki-agent` (Family Wiki reads/prefetch).
- `jq`, `curl`, `flock` (standard).
- A Honcho endpoint reachable from the node (set in `honcho.json`).
