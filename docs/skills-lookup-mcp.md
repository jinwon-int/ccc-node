# Skill lookup — JSON CLI and `family-skills` MCP (#1678)

One deterministic, fail-closed implementation serves skill search and read to
every consumer: the JSON CLI, the `family-skills` stdio MCP server, and the
CCC Claude bridge injection. Wiki access reuses the existing `family-wiki`
server (`wiki-agent mcp-serve`) and is never reimplemented here.

## Components

| Path | Role |
|---|---|
| `bridge/core/skill_lookup.py` | Common search/read logic. stdlib-only; runs under any python3, inside or outside the bridge venv. |
| `bridge/core/family_skills_server.py` | `family-skills` stdio MCP server (JSON-RPC 2.0, newline-delimited). stdlib-only. |
| `bridge/core/family_mcp.py` | Bridge-side explicit injection for owner profiles; merges with the curated web MCP without overwriting it. |
| `scripts/ccc-skill-lookup.py` | JSON CLI. `stdout` carries results only; diagnostics go to `stderr`. |
| `claude/mcp-setup.sh` | Idempotent user-scope registration of `family-skills` and `family-wiki` for Claude CLI. |

## Sources, ids, and revisions

Approved roots only; no other OS user's home is scanned:

- **repo** — `skills/registry.json` (CI-enforced truth), sources under
  `skills/`, `codex/skills/`, `piri/skills/`. The registry is a derived view,
  so every read re-validates the actual files. The same skill *name* may exist
  under two sources, so repo ids carry the source path:
  `repo:skills/gh-pr-flow`, `repo:codex/skills/gh-pr-flow`.
- **installed** — `~/.claude/skills`, `${CODEX_HOME:-~/.codex}/skills`,
  `~/.piri/agent/skills`. Ids are `<runtime>:<name>` (e.g. `claude:gh-pr-flow`).

Search results advertise a `revision`: the registry tree hash (recomputed from
the worktree with the registry's own git-first enumeration) for repo entries,
the SKILL.md sha256 for installed entries. `skill_read` re-verifies path
containment, symlinks, ownership, permission bits, size (≤128 KiB), UTF-8 and
frontmatter/name agreement, and returns `stale_revision` instead of silently
serving content that no longer matches the advertised revision.

Search order is deterministic: repo sources (sorted) first, then installed
runtimes in `claude`, `codex`, `piri` order. Deprecated/non-active entries are
excluded from search but remain readable by exact id. Match is all-tokens,
case-insensitive, against name + description.

## Tool / CLI contract

| Interface | Input | Result |
|---|---|---|
| `skill_search` | `query` (1..256 chars, required), `runtime` (optional: `repo|claude|codex|piri`), `limit` (optional, 1..50, default 10) | `results[]` of `skill_id`, `name`, `description`, `runtime`, `audience`, `source`, `active`, `revision`; plus `truncated` |
| `skill_read` | `skill_id` (required), `revision` (optional expected 64-hex) | `skill_id`, `name`, `runtime`, `source`, `revision`, `description`, `bytes`, `body` |
| JSON CLI | same inputs (`search --query … [--runtime …] [--limit …]`, `read --id … [--revision …]`) | JSON on stdout; structured `{"error": {code, message, …}}` with exit 1 on failure |

Structured error codes: `policy_denied`, `invalid_query`, `invalid_skill_id`,
`invalid_revision`, `not_found`, `stale_revision`, `unsafe_skill`,
`invalid_content`, `registry_invalid`, `unknown_tool`.

CLI examples:

```bash
python3 scripts/ccc-skill-lookup.py search --query "pr flow" --limit 3
python3 scripts/ccc-skill-lookup.py read --id repo:skills/gh-pr-flow
```

MCP server (registration happens in `claude/mcp-setup.sh`):

```bash
claude mcp add family-skills -s user -- python3 /opt/ccc-node/bridge/core/family_skills_server.py
claude mcp add family-wiki   -s user -- wiki-agent mcp-serve
```

Protocol flow: `initialize` (echoes a supported protocol version) →
`tools/list` → `tools/call`. Results are returned as a single JSON text
content block; domain failures are `isError: true` tool results, not
transport errors. Frames over 1 MB and undecodable lines are answered with
JSON-RPC protocol errors. Diagnostics on stderr contain tool names, ids and
counts only — never queries, arguments, or skill bodies.

## Access policy

Decided from the process environment of the server/CLI (never from tool
arguments, so a caller cannot self-declare an audience), re-checked at every
`tools/call`:

| Context | family-skills | family-wiki (bridge injection + mcp-setup) |
|---|---|---|
| owner (`fleet` profile, no audience or `private`) | allowed | allowed when `wiki_memory_enabled` and `wiki-agent` installed |
| `CCC_MEMORY_AUDIENCE=shared` | denied | not injected |
| `CCC_NODE_ISOLATION_PROFILE=external` | denied | not injected |

Because the server re-checks the same environment at call time, a user-scope
registration or a direct stdio connection cannot bypass the node's isolation
profile. The bridge injects the servers only for owner-operator profiles with
`setting_sources=[]` (unrestricted or audience-scoped-private); plain owner
sessions keep the host settings chain and see the user-scope registration
natively. Shared-audience and non-owner profiles get neither server.

Tool permissions `mcp__family-skills__*` and `mcp__family-wiki__*` are
pre-allowed in `claude/settings.base.json`.

## Danso (and other bash-capable runtimes)

Danso consumes the JSON CLI through its permitted bash tool:

```bash
python3 "${CCC_NODE_ROOT:-/opt/ccc-node}/scripts/ccc-skill-lookup.py" \
  search --query "incident" --limit 5
```

This is a CLI integration only. Danso has no MCP client support today; do not
represent native MCP connectivity for Danso (or Codex/Piri) as done — those
are separate, later work items.

## Uninstall / teardown

```bash
claude mcp remove family-skills -s user
claude mcp remove family-wiki -s user
```

The servers are plain repo files; deleting the checkout removes them. On
failure the CLI/MCP return structured errors and exit — no partial results,
no silent truncation (an over-limit file is an error, not a truncated body).

## Scope notes

- First implementation covers skill search/read + wiki reuse + Claude CLI and
  CCC Claude bridge consumption. Node status, task recovery, PR readiness,
  deployment diff and incident search are follow-up roadmap items tracked in
  separate issues.
- Wiki tools come verbatim from `wiki-agent mcp-serve` (read-only `wiki_find`,
  `wiki_load`, `wiki_prefetch`); no wiki logic lives in this repo.
- Skill bodies are reference documentation. Nothing here executes skills or
  grants approvals, merges, restarts, or credential access.
