# Security audit operations

`ccc-security-audit.sh` is a read-only metadata-oriented checker for ccc-node harness safety. It should classify risk without printing matched secret values, memory contents, raw environment, or raw Telegram/provider payloads.

## Local use

Run the local checker from the repo or installed node surface:

```bash
bash scripts/ccc-security-audit.sh
```

Use JSON output where available for automation and fleet rollups.

## Fleet rollup

Collect per-node output into block evidence and summarize with:

```bash
bash scripts/ccc-security-audit-fleet-matrix.sh --evidence fleet-security.txt --json
```

The fleet matrix is read-only. It does not SSH, change permissions, restart services, send providers, read secrets, or mutate node state. It classifies already-collected evidence into `정상`, `경고`, `교정가능`, `수동필요`, or `위험`.

## Reporting boundary

Report counts, statuses, file modes, safe reason tags, and remediation class. Do not include raw matched lines if they may contain credentials or private memory.

## Agent trust surface (#2538)

Background: a web page can silently drop an `AGENTS.md` / `CLAUDE.md` into
`~/Downloads`; a Codex or Claude Code session started there follows it as
instructions (forever.security, 2026-10-22). Reproduced payloads added an MCP
server to `~/.codex/config.toml` (malicious npm `function-color`) and set
`ANTHROPIC_BASE_URL` in `.claude/settings.json` (OAuth token leak). The fleet
variant: A2A analysis sessions run under `/tmp/a2a-analysis-*`, and Claude Code
loads `CLAUDE.md` from every ancestor directory, so one `/tmp/CLAUDE.md` would
reach every worker session on a node.

`claude/hooks/lib/agent_trust.py` is the shared, stdlib-only collector. It is
**detection only** — TM-1306 keeps blocking guards and `permissions.deny` out
of the harness. Output is hosts, paths and hashes; never file contents or
secret values.

| Severity | Finding | Meaning |
|---|---|---|
| 위험 / danger | `download-agent-file` | agent instruction/config file in a download dir, outside git |
| 위험 / danger | `ancestor-agent-file` | `AGENTS.md`/`CLAUDE.md` directly in `/`, `/tmp`, `/var/tmp`, `$PREFIX/tmp`, or a home parent |
| 위험 / danger | `known-bad-npm` | global `node_modules/function-color` |
| 경고 / warn | `endpoint-override` | Claude `env.*BASE_URL` / Codex `*base_url` host not in the allowlist (`CCC_AGENT_TRUST_ENDPOINT_ALLOW`, comma-separated hosts) |
| 경고 / warn | `unpinned-npx-mcp` | MCP server launched as `npx -y <pkg>` without an exact version |
| 경고 / warn | `home-agent-file` | unmanaged `AGENTS.md`/`CLAUDE.md` in a home dir (loaded by every bridge session) |
| 경고 / warn | `ancestor-agent-config`, `session-in-downloads`, download file inside a git checkout | review |

```bash
python3 claude/hooks/lib/agent_trust.py          # human summary
python3 claude/hooks/lib/agent_trust.py --json   # ccc.agent-trust.v1
bash scripts/ccc-security-audit.sh               # includes "agent trust" rows
```

### Weekly fleet sweep (hub only)

`scripts/agent-trust-audit-fleet.sh` pipes the collector to every node over ssh
(`python3 - --json`, independent of the remote checkout's age) and diffs each
node's `signature` (endpoint hosts, MCP servers, home instruction files,
findings) against an accepted baseline under
`~/.claude/state/agent-trust-audit/`. Verdicts: `OK`, `DANGER` (always fails,
even when accepted), `NEW` (fails), `GONE`, `UNBASELINED`, `UNREACHABLE`,
`ERROR`.

```bash
CCC_FLEET_NODES="<ssh aliases>" bash scripts/agent-trust-audit-fleet.sh   # review
CCC_FLEET_NODES="<ssh aliases>" bash scripts/agent-trust-audit-fleet.sh --accept-baseline
bash scripts/install-agent-trust-audit-cron.sh --apply --nodes "<ssh aliases>"   # Mon 06:40, hub only
```

If a finding is real: do not run agents in that directory, delete the planted
file, review `~/.codex/config.toml` and Claude settings for MCP/endpoint
changes, and rotate the agent OAuth/API tokens.
