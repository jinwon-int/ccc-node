# ccc-node

**CCC = Claude Code Cli** — the unified node-setting monorepo for turning a Seoyoon/Hermes
node into a **Claude Code node (클코 노드)**: the same harness configuration `nosuk` (VPS2)
runs, plus the Telegram bridge that connects Claude Code to Telegram, with all secrets and
node-local state stripped out and replaced by placeholders.

> Captured from the `nosuk` node setup on **2026-06-19** so other nodes can be bootstrapped
> the same way. This repo holds the **mechanism**, not any node's live secrets/memory.
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
and slash commands — is also packaged as a Claude Code plugin (`.claude-plugin/`):

```bash
/plugin marketplace add jinwon-int/ccc-node
/plugin install ccc-node@ccc-node
```

The plugin and `setup.sh` are complementary: the **plugin** carries the portable surface,
while **`setup.sh`** installs the node-local memory bootstrap (SessionStart/PostCompact
injection, working-state checkpoint) that is inherently node-specific. See `CHANGELOG.md`.

## Layout

```
claude/
  settings.json            # permissions + SessionStart/PostCompact hooks (secret-free)
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
