# Changelog — ccc-node harness

All notable changes to the Claude Code node harness. Dates are KST.

## [0.1.1] — 2026-06-20

### Fixed
- **Plugin now actually loads.** The 0.1.0 manifest passed `claude plugin validate` but failed at install time (`Status: ✘ failed to load`). Two distinct defects, both confirmed by a real isolated install on Claude Code 2.1.183:
  1. `plugin.json` referenced `./hooks/hooks.json` in its `hooks` field, but `hooks/hooks.json` is auto-loaded — the duplicate reference aborted the whole plugin load.
  2. `agents`/`commands` custom-path **arrays** (`./claude/...`) are schema-valid but silently load **0** components in this CLI; only default-location discovery is honoured.
- **Fix**: the marketplace entry now points the plugin root at the existing component tree via `source: "./claude"`. The manifest moves to `claude/.claude-plugin/plugin.json` with **no path fields** (agents/commands/skills/hooks auto-discovered), and the hook config moves to `claude/hooks/hooks.json` with `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` paths. No `claude/` restructure; `setup.sh` is unaffected.
- Verified real install: `Status: ✔ enabled` — Skills 7 (incl. 3 commands), Agents 4, Hooks 6, all hook scripts resolve.
- **Validator hardened** (`scripts/validate-harness.sh`): now resolves every `${CLAUDE_PLUGIN_ROOT}` hook path to an on-disk script, rejects silent-load path fields, asserts `source: "./claude"`, and runs `claude plugin validate` when the CLI is present — the checks that would have caught 0.1.0.

## [0.1.0] — 2026-06-20

First versioned/packaged release. Installable as a Claude Code **plugin** (`/plugin marketplace add jinwon-int/ccc-node` → `/plugin install ccc-node@ccc-node`) in addition to the existing `setup.sh` bootstrap.

### Added
- **Plugin packaging**: `.claude-plugin/plugin.json` manifest + `.claude-plugin/marketplace.json` catalog + `hooks/hooks.json` (enforcement + observability hooks via `${CLAUDE_PLUGIN_ROOT}`). Packages the node-agnostic surface (skills, slash commands, A2A agents, guard/audit/redact/notify hooks).
- **Tier 1 enforcement** — `guard.sh` PreToolUse fail-closed guard for the Fresh-Approval set, with `CCC_ALLOW_GATED=1` operator escape hatch; risk-profile mapping (`RISK-PROFILES.md`); `permissions.deny`/`ask`.
- **Tier 1.5 observability** — `audit.sh` (PostToolUse, secret-redacted JSONL), `redact.sh` (UserPromptSubmit secret-awareness), `notify.sh` (Notification/Stop/SessionEnd; approval-needed log + working-state archive).
- **Tier 2** — harness CI (`scripts/validate-harness.sh` + `.github/workflows/ci.yml`); slash commands `/node-status`, `/a2a-claim`, `/wiki-log`.
- Skills: `wiki-record`, `mcp-add`, `skill-suggest`, `gh-pr-flow`.
- A2A worker sub-agent roster: `a2a-explorer`, `a2a-researcher`, `a2a-implementer`, `a2a-verifier`.

### Notes
- **Node-local memory bootstrap** (SessionStart/PostCompact memory injection, working-state checkpoint) stays in `setup.sh` — it is inherently node-specific and not part of the portable plugin.
- Two install paths coexist: plugin (portable surface) + `setup.sh` (memory bootstrap + node templates).
