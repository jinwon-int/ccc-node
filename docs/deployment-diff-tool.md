# Deployment diff tool — pre-deployment snapshot (CLI + `family-ops` MCP, #1694 item 4)

One read-only call aggregates what a deployment pre-check scans for, by
**reusing this repo's existing sources** instead of reimplementing them. Same
structure as the other `family-ops` tools (`docs/node-status-tool.md`,
`docs/task-status-tool.md`, `docs/pr-readiness-tool.md`): a stdlib-only
module (`bridge/core/deployment_diff.py`), the JSON CLI
`scripts/ccc-deployment-diff.py`, and the `deployment_diff` tool on the
`family-ops` MCP server — one tool per roadmap item.

## Sections

| Section | Source | Content |
|---|---|---|
| `checkout` | `scripts/ccc-bridge-locate.sh --json` (same as `node_status`) | running bridge pid/checkout/head/branch/dirty |
| `target` | `git ls-remote origin main` | target SHA (no fetch needed) |
| `history` | bounded `git fetch` + `rev-list`/`status` | ahead/behind counts, dirty-file count, `fetched` flag (`behind: null` + note when unknown) |
| `installed` | `<claude_dir>/state/self-update.installed-sha` + `scripts/ccc_doctor.py --json` | last-installed marker (sha, age) and harness drift rows aggregated (status counts, drifted/missing item names capped at 20) |
| `dependencies` | `git diff --name-only HEAD..origin/main -- bridge/requirements*.txt pyproject.toml` | changed dependency files + serving venv interpreter version |
| `recovery` | `<claude_dir>/backups/ccc-node-setup-*.tar.gz` | latest snapshot name/path/size/age, total count |

The installed-state comparison **delegates to `ccc_doctor.py`**, exactly like
`skills/self-update/check.sh` does: commit distance alone never proves harness
currency (#1033 phantom-drift lesson). `fetch` follows the same script's
read-only precedent; `CCC_DEPLOYMENT_DIFF_FETCH=0` disables it (behind count
becomes `null` with a note, target equality still works via `ls-remote`).

## Deployment verdict

`deployment` is an informational aggregation (`verdict:
up_to_date|restart_recommended`, `reasons[]`, `informational_only: true`).
Reasons include `checkout_behind:N`, `checkout_dirty:N`,
`bridge_running_old_code` (only when the serving bridge's checkout path is the
same repo root and its head differs from the local head),
`installed_drift:N`, `installed_marker_differs`, `installed_marker_missing`,
plus `*_unknown` markers for failed sections. It mirrors the update flow — it
does not replace it: `self-update` approvals, `setup.sh`, restart preflights,
and live re-verification keep every rule they ever had.

## CLI and MCP

```bash
python3 scripts/ccc-deployment-diff.py
```

MCP: `mcp__family-ops__deployment_diff` (no arguments) on the `family-ops`
server — same call-time node-policy gate (external/shared denied) as the
other tools.

## Principles

- Strictly read-only: no pull, install, setup, restart, backup rotation, or
  approval. `git fetch` updates remote-tracking refs only — the drift
  precedent from `skills/self-update/check.sh`.
- The marker path deliberately ignores `CCC_STATE_DIR`: inside a bridge
  session that variable points at the memory-audience state dir, while
  self-update (cron, clean env) writes the marker under the harness home.
  Test seam `CCC_DEPLOYMENT_DIFF_MARKER_FILE` overrides it explicitly.
- Partial failures are `unknown`, never fabricated; observation time and
  latency are in every result; the server logs counts to stderr, never
  bodies.

## Scope notes

- Dependency diff needs the fetched objects; with fetch disabled it reports
  the skip reason instead of guessing.
- Test seams: `CCC_DEPLOYMENT_DIFF_LOCATE`, `CCC_DEPLOYMENT_DIFF_DOCTOR`,
  `CCC_DEPLOYMENT_DIFF_GIT` (space-separated command overrides),
  `CCC_DEPLOYMENT_DIFF_FETCH`, `CCC_DEPLOYMENT_DIFF_MARKER_FILE`, plus the
  shared `CCC_CLAUDE_DIR` / `CCC_SKILL_LOOKUP_REPO_ROOT` variables.
