# Incident find tool — evidence search (CLI + `family-ops` MCP, #1694 item 5)

One read-only call combines the node's existing search sources for one
symptom/error query — the #1694 roadmap's final item ("유사 오류·확인된 원인·
해결 PR·적용 노드·근거 문서 — Wiki/GitHub 검색 조합"). Same structure as the
other `family-ops` tools: a stdlib-only module (`bridge/core/incident_find.py`),
the JSON CLI `scripts/ccc-incident-find.py`, and the `incident_find` tool on
the `family-ops` MCP server.

Boundary: `family-skills`' `skill_search`/`skill_read` answer "which skill"
from the skill catalog; `incident_find` searches operational evidence across
the whole Wiki plus GitHub.

## Sections

| Section | Source | Content |
|---|---|---|
| `wiki` | `wiki-agent find --json --top 5 <query>` | top semantic sections (path/heading/bounded snippet/score/load command), text matches, `abstained` + `confidence` preserved |
| `issues` | `gh search issues <query> --repo <repo> --limit 10` (optional repo) | title/url/state |
| `pull_requests` | `gh search prs <query> --repo <repo> --limit 10` | title/url/state |

With no `repo`, the GitHub sections report `skipped: true` ("no repo given")
instead of guessing a scope. wiki-agent's own contract is preserved: results
are **candidates** — every entry carries its `load_command` and the section
carries a verify-before-claims note.

## Principles

- Read-only: two `gh search` reads and one `wiki-agent find`; nothing is
  created, edited, resumed, or approved.
- Results are candidates only (`results_are_candidates_only: true`); reading
  the underlying evidence — and any operational decision — stays a separate
  human step. Abstention is never overridden: a low-confidence wiki search
  reports `abstained: true` instead of padding with weak matches.
- Partial failures are section-scoped `unknown`; the family-ops call-time
  node-policy gate (external/shared denied) applies as for every tool.

## CLI and MCP

```bash
python3 scripts/ccc-incident-find.py --query "symptom keywords" [--repo OWNER/REPO]
```

MCP: `mcp__family-ops__incident_find` (`query` required, `repo` optional).

## Scope notes

- Query is capped at 512 chars; snippets at 240 chars; result caps are fixed
  (5 wiki / 10 per GitHub search).
- Test seams: `CCC_INCIDENT_FIND_WIKI` and `CCC_INCIDENT_FIND_GH`
  (space-separated command-prefix overrides) plus the injected runner in
  unit tests.
