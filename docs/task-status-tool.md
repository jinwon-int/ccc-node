# Task status tool — recovery aggregation (CLI + `family-ops` MCP, #1694 item 2)

One read-only call aggregates what resuming work costs the most: the agent
checkpoint, the resume note, and external wait promises. Same structure as
`node_status` (see `docs/node-status-tool.md`): a stdlib-only module
(`bridge/core/task_status.py`), the JSON CLI `scripts/ccc-task-status.py`,
and the `task_status` tool on the existing `family-ops` MCP server — one
tool per roadmap item.

## Sections

| Section | Source | Content |
|---|---|---|
| `working_state` | `$CCC_STATE_DIR/working-state.md`, else `~/.claude/state/working-state.md` | objective / progress / next step (bounded 8 KiB) |
| `resume` | `CCC_RESUME_FILE`, else `resume.md` in the same state dir | resume note (bounded 8 KiB) |
| `waits` | `external_wait_cli list` (subprocess, JSON) | `active` (pending promises) and `dropped` (terminal but never resumed, with `skip_reason`) |

Every section reports `status: ok|unknown` plus, for files, `present`,
`path`, `size_bytes`, `modified_at`, `age_hours`. Bounded content carries
`truncated: true` when the file exceeded the bound — truncated content is
never presented as complete. A failed waits collector is section-scoped
`unknown` and never blocks the notes.

Wait classification: non-terminal (`pending`) → `active`;
terminal (`success`/`failure`/`superseded`) with `resumed: false` →
`dropped` (the SessionStart "미완 약속" triage inputs). Terminal-and-resumed
waits are counted in `total` but not listed.

## CLI and MCP

```bash
python3 scripts/ccc-task-status.py
```

MCP: `mcp__family-ops__task_status` (no arguments) on the `family-ops`
server — already registered and injected wherever `family-skills`/`node_status`
are, with the same call-time node-policy gate (external/shared denied).

## Principles

- Strictly read-only: this tool never resumes, replays, mutates, or
  approves anything. It lowers the cost of *finding* recovery state; acting
  on it keeps every existing approval and live re-verification rule.
- Partial failures are `unknown`, never fabricated; observation time is in
  every result.
- Content is owner-context memory: bounded, served only through the node
  policy gate, and never logged by the server (stderr carries counts only).

## Scope notes

- Remote (ssh) aggregation and background-task registries are not in this
  item; they can follow the `node_status` remote pattern later.
- Test seams: `CCC_TASK_STATUS_WAITS_CMD` (space-separated command override)
  plus the shared `CCC_STATE_DIR` / `CCC_SKILL_LOOKUP_HOME` / repo-root
  variables.
