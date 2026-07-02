# Harness settings and node-local surface

This document covers the Claude Code harness pieces installed by `setup.sh`: settings, hooks, status line, output style, plugin/standalone mode, and non-root path overrides.

## Installed surface

| Path | Purpose |
|---|---|
| `claude/settings.base.json` | Node-local hooks, `statusLine`, and `outputStyle` baseline. |
| `claude/settings.local.json` | Local permission allowlist template. |
| `claude/hooks/enforcement-overlay.json` | Portable enforcement/observability hook overlay for standalone installs. |
| `claude/CLAUDE.md.template` | Operating-policy skeleton with placeholders for node/user identity. |
| `claude/hooks/` | Memory loading, tool loading, guard/audit/redact/notify, distill, skill-review, statusline, evidence gate. |
| `claude/output-styles/ccc-report.md` | Korean structured-reporting default. |
| `hermes/` | Hermes-side templates for memory and Honcho config; real values stay node-local. |

## Standalone vs plugin mode

The portable enforcement/observability hooks must have one owner. Claude Code does not de-duplicate hooks, so avoid double-firing.

| Mode | How to install | Hook owner |
|---|---|---|
| Standalone (default) | `./setup.sh` | `settings.json` = base + enforcement overlay |
| Plugin | `./setup.sh --with-plugin`, then install the plugin | Plugin owns portable hooks; `settings.json` keeps node-local base hooks |

Plugin marketplace:

```text
/plugin marketplace add jinwon-int/ccc-node
/plugin install ccc-node@ccc-node
```

`validate-harness.sh` asserts the overlay and plugin `hooks/hooks.json` stay equivalent.

## Status line

`hooks/statusline.sh` emits a compact Claude Code status line:

- node label (`CCC_NODE` or `~/.claude/state/node.txt`, fallback short hostname)
- model/git/context/cost information when available
- large-context marker such as `⚠200k`
- A2A marker and output-style cue

It is wired through `settings.json` `statusLine`.

## Output style

`output-styles/ccc-report.md` is the Korean structured-reporting default:

- 확정 / 변경 / 리스크 / 다음
- concise progress narration
- numbered-choice questions

Switch interactively through Claude Code `/config` → Output style when needed.

## Path overrides

`setup.sh` defaults to root-compatible `$HOME` paths but supports explicit non-root paths:

| Variable | Default | Purpose |
|---|---|---|
| `CCC_CLAUDE_DIR` | `$HOME/.claude` | Claude Code harness, hooks, memories, output styles, commands, skills |
| `CCC_HERMES_DIR` | `$HOME/.hermes` | `honcho.json` and Hermes-side local config templates |
| `CCC_WIKI_AGENT_BIN` | `$HOME/.wiki-agent/bin/wiki-agent` | Printed checklist path for Family Wiki tooling |
| `CCC_BRIDGE_DEFAULT_PATH` | `$HOME` | Suggested Telegram bridge workspace in setup output |
| `CCC_STATE_DIR` | `$CCC_CLAUDE_DIR/state` | State files plus local `memory-index.sqlite` |
| `CCC_MEMORY_CACHE_DIR` | `$CCC_CLAUDE_DIR/hooks/cache` | Wiki/Honcho cache and refresh metadata |

Example preview:

```bash
HOME=/home/ccc \
CCC_CLAUDE_DIR=/home/ccc/.claude \
CCC_HERMES_DIR=/home/ccc/.hermes \
CCC_WIKI_AGENT_BIN=/home/ccc/.wiki-agent/bin/wiki-agent \
CCC_BRIDGE_DEFAULT_PATH=/home/ccc \
./setup.sh --dry-run
```

## Validation

```bash
bash scripts/validate-harness.sh
scripts/ccc-doctor.sh
scripts/ccc-version.sh
```

For bridge-specific checks, see [`bridge/README.md`](../bridge/README.md) and [`bridge/start.sh --status`](../bridge/start.sh).
