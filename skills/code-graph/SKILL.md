---
name: code-graph
description: Use when you need who-calls-what or blast-radius answers for a jinwon-int Python repo — callers/callees of a function, a module's imports, or the files that depend on a changed file (e.g. choosing review-packet files). Builds a local per-commit ast call/import graph; calls are tagged exact or name-match.
---

# code-graph — local static call/import graph (stage 1: Python)

A stdlib-only script (`ast`, no third-party packages, no server) builds a JSONL
graph per commit and answers four queries. It replaces repeated ad-hoc `grep`
for "who calls this" and "what breaks if I change this file". Stage 1 covers
Python only; other languages are not graphed yet.

```bash
G=<skill-root>/scripts/code_graph.py   # <skill-root> = the directory holding this SKILL.md
python3 "$G" build <repo-path> [--repo] [--worktree] [--force] [--json]
python3 "$G" callers <symbol>      [--path <repo>] [--json]
python3 "$G" callees <symbol>      [--path <repo>] [--json]
python3 "$G" imports-of <module|file.py> [--reverse] [--path <repo>] [--json]
python3 "$G" impact <file.py>...   [--depth 2] [--path <repo>] [--json]
```

Queries build the graph on demand under the same gates (pass the same
`--repo`/`--worktree` flags). Exit codes: 0 ok, 1 error, 2 usage, 3 refused.

## Gates (why a build can be refused)

- **Opt-in.** The repo root has `.ccc-code-graph.toml`, or you pass `--repo`.
  The file holds glob lists (default include `**/*.py`, exclude none):
  `include = ["**/*.py"]` and `exclude = ["vendor/**", "tests/fixtures/**"]`.
- **Fleet repos only.** `origin` must be `github.com/jinwon-int/*`. Anything
  else is refused: outside code is data, not something to index and act on
  (see the `external-github-content` skill).
- **Clean tree.** The cache key is `git rev-parse HEAD`. A tree with tracked
  changes is refused unless `--worktree`; then the key gets a `+dirty-<hash>`
  suffix that changes with the content, so a dirty graph is never reused for a
  different edit. Untracked files are ignored unless `--worktree`.

Cache: `~/.cache/ccc-code-graph/<owner>__<repo>@<sha12>.jsonl` (override with
`--cache-root` or `CCC_CODE_GRAPH_CACHE`; `XDG_CACHE_HOME` is honoured). Files
are 0600, the newest 8 per repo are kept, and a new HEAD means a new build.

## Reading the graph

- One JSON object per line. Line 1 is `meta` (sha, file count, skipped
  files). Then `node` lines (`module`, `class`, `function`) and `edge` lines
  (`imports`, `defines`, `calls`). Every line has `at: "path:line"`.
- Ids are dotted paths from the repo root (`bridge/core/x.py` →
  `bridge.core.x`, a method → `bridge.core.x.Class.method`).
- `calls` edges carry `confidence`:
  - `exact`: the name resolves through lexical scope (same module, enclosing
    function) or an explicit import, following package re-exports, to exactly
    one definition in the graph.
  - `name-match`: a best guess, never proof. Covers `obj.method()`,
    `self.method()` (a subclass may override it; dynamic dispatch is not
    resolved), and calls into modules outside the repo (`external: true`).
    Bare-name guesses carry `candidates` (how many definitions share the name).
- Absolute imports resolve the way `sys.path[0]` does: the importer's own
  directory first, then each parent up to the repo root, then installed-name
  mappings: `[tool.setuptools] package-dir` in any tracked `pyproject.toml`
  (e.g. `{"pkgname" = "."}` in `sub/` maps `pkgname.x` to `sub/x.py`) and a
  `packages = {pkgname = "sub"}` table in `.ccc-code-graph.toml`.
- Files that fail to parse are skipped and listed in `meta.skipped`; the build
  never stops on one bad file.

## Using the answers

- `callers foo` matches `foo`, any `*.foo` id, and bare `foo` name-match
  edges. Check the `name-match` rows by reading the code before relying on
  them; treat `exact` rows as strong but still static (no runtime tracing).
- `impact a.py b.py` walks reverse imports (default depth 2) and lists
  dependent files with the file they reach the change through. Use it to pick
  review or test scope; it does not see dynamic imports or subprocess calls.
- Report the graph's sha (`build` prints it) with any claim made from it.
