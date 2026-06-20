# Changelog — ccc-node harness

All notable changes to the Claude Code node harness. Dates are KST.

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
