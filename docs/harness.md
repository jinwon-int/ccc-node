# Harness settings and node-local surface

This document covers the Claude Code harness pieces installed by `setup.sh`: settings, hooks, status line, output style, plugin/standalone mode, and non-root path overrides.

## Installed surface

| Path | Purpose |
|---|---|
| `claude/settings.base.json` | Node-local hooks, `statusLine`, and `outputStyle` baseline. |
| `claude/settings.local.template.json` | Local permission allowlist template. |
| `claude/hooks/enforcement-overlay.json` | Portable enforcement/observability hook overlay for standalone installs. |
| `claude/CLAUDE.md.template` | Operating-policy skeleton with placeholders for node/user identity. |
| `claude/hooks/` | Memory loading, tool loading, guard/audit/redact/notify, distill, skill-review, statusline, evidence gate. |
| `claude/output-styles/ccc-report.md` | Korean structured-reporting default. |
| `hermes/` | Hermes-side local config templates; real values stay node-local. |

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

## Hook python: shared `ccc_secure_fs` import convention

`setup.sh` installs the canonical `bridge/utils/secure_fs.py` verbatim as
`~/.claude/hooks/ccc_secure_fs.py` (`scripts/setup.test.sh` `cmp`-guards the
copy), so every installed hook module shares one implementation of the
owner-only read / JSONL / atomic-replace / flock / clock helpers instead of
re-implementing them (#1484, #1503, #1508).

- Scripts installed directly into `~/.claude/hooks/` (e.g. `ccc-skill-promotion.py`)
  simply `import ccc_secure_fs`: Python puts the script's own directory first
  on `sys.path`.
- Hook modules installed into a subdirectory (`~/.claude/hooks/skill-review/`,
  `~/.claude/hooks/nunchi/`, ...) insert their hooks root —
  `Path(__file__).resolve().parents[1]` — at the front of `sys.path` and then
  `import ccc_secure_fs`. `claude/hooks/ccc_secure_fs.py` is generated at install
  time and does not exist in the repository tree, so the same block falls back
  to loading `bridge/utils/secure_fs.py` (`parents[3]`) under the module name
  `ccc_secure_fs`; repo tests therefore exercise the identical bytes `setup.sh`
  installs. Siblings that already import a converted module (`curator.py` via
  `ownership.py`; `judge-batch.py`/`wiki-promote.py` via `nunchi.py`) rely on
  that registration and use the plain import.
- Keep hook-local wrappers only where the contract differs: the module maps
  `SecureFsError.reason` onto its own error codes (`unsafe_metadata`,
  `unsafe_ownership_ledger`, `ownership_ledger_changed`, ...) rather than leaking
  the shared exception, and directory-descriptor-relative writes go through
  `atomic_write_bytes_at` after the caller has validated the directory.

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
| `CCC_HERMES_DIR` | `$HOME/.hermes` | Hermes-side local config templates |
| `CCC_WIKI_AGENT_BIN` | `$HOME/.wiki-agent/bin/wiki-agent` | Printed checklist path for Family Wiki tooling |
| `CCC_BRIDGE_DEFAULT_PATH` | `$HOME` | Suggested Telegram bridge workspace in setup output |
| `CCC_STATE_DIR` | `$CCC_CLAUDE_DIR/state` | State files plus local `memory-index.sqlite` |
| `CCC_MEMORY_CACHE_DIR` | `$CCC_CLAUDE_DIR/hooks/cache` | Wiki cache and refresh metadata |

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
