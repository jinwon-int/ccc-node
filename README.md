# ccc-node

**CCC = Claude Code Cli** — the unified node-setting monorepo for turning a Seoyoon/Hermes
node into a **Claude Code node (클코 노드)**: the same harness configuration proven on
`nosuk` (VPS2) and `soonwook` (VPS6), plus the Telegram bridge that connects Claude Code
to Telegram, with all secrets and node-local state stripped out and replaced by placeholders.

> Captured from the `nosuk` node setup on **2026-06-19** and updated after the `soonwook`
> VPS6 rollout on **2026-06-20** so other nodes can be bootstrapped the same way. This repo
> holds the **mechanism**, not any node's live secrets/memory.
>
> **Bridge provenance:** `bridge/` was vendored from a fork of
> [`terranc/claude-telegram-bot-bridge`](https://github.com/terranc/claude-telegram-bot-bridge)
> (commit history preserved). The upstream relationship is **intentionally dropped** — the
> bridge is now developed here, independently, as part of `ccc-node`.

## What this gives a node

- **SessionStart / PostCompact memory injection** — `hooks/load-memory.sh` injects a bounded
  snapshot at session start: built-in `MEMORY.md`/`USER.md` + local hot-memory search
  (on by default for every profile; `CCC_LOCAL_MEMORY_ENABLED=0` opts out) + cached Family
  Wiki prefetch + cached Honcho working memory, then fires a detached background refresh for
  the next session. Startup remains no-network/fail-open.
- **Background cache refresh** — `hooks/refresh-memory.sh` updates the Wiki + Honcho caches
  out-of-band (single-flight via `flock`, fail-open), records per-source cache metadata,
  and opportunistically updates the local SQLite FTS5 hot-memory index.
- **Local memory diagnostics/eval** — `scripts/ccc-memory-check.sh`,
  `scripts/ccc-memory-index.sh`, `scripts/ccc-memory-search.sh`,
  `scripts/ccc-memory-query.sh`, `scripts/ccc-memory-explain.sh`,
  `scripts/ccc-wiki-triage.sh`, `scripts/ccc-memory-eval.sh`, and
  `scripts/ccc-memory-benchmark-export.sh` provide cache health, task-aware query construction,
  SQLite FTS5 indexing/search with docs-only fallback, hybrid-local scoring diagnostics,
  read-only recall explanations, human-gated Wiki candidate triage, no-network smoke/golden/scenario
  quality tests, and disabled-by-default synthetic benchmark export for `CCC_MEMORY_PROFILE=hybrid|max-perf`.
- **Harness settings** — `settings.json` (permissions + hook wiring) and `settings.local.json`.
- **CLAUDE.md template** — the operating-policy skeleton (Wiki-first, A2A/Nexus, GitHub
  hygiene, fresh-approval rules) with node/user identity as `<PLACEHOLDERS>`.
- **Telegram bridge** (`bridge/`) — a lightweight bot that bridges Claude Code to Telegram
  for any local folder, with autostart/supervisor support. Run via `bridge/start.sh`.
- **Push notifier** (`bridge/core/push_notifier.py`) — opt-in, owner-only delivery of
  Claude Code lifecycle notifications. The notify hook (`CCC_NOTIFY_TELEGRAM=1`) writes
  redacted summaries to a spool; the bridge sends them so the **bot token never leaves the
  bridge**. Disabled by default; enable via `CCC_PUSH_ENABLED` + an owner chat id.
- **Status line** (`hooks/statusline.sh`) — node · model · git · context % · `⚠200k` · cost ·
  A2A marker · output style, wired via `settings.json` `statusLine`. Set `CCC_NODE` (or
  `~/.claude/state/node.txt`) for the node label; falls back to the short hostname.
- **Output style** (`output-styles/ccc-report.md`) — Korean structured-reporting default
  (확정/변경/리스크/다음, 진행 내레이션, 번호형 질문), activated via `settings.json`
  `outputStyle`. Switch anytime with `/config` → Output style.
- **Doctor diagnostics** (`scripts/ccc-doctor.sh`, `/doctor`) — harness drift classification for
  settings, hook wiring, output style, status line, and bridge status. `--fix` is dry-run by
  default; `--fix --apply` defaults to scoped `settings.json` repair after a backup tar, while
  `--fix --apply --scope=files` reinstalls only allowlisted hook/output-style files after a
  scoped backup and refuses symlinks/path escapes/plugin double-firing risk. `--rollback` is
  dry-run by default, and `--rollback --apply` restores only `settings.json` from the latest
  doctor backup after creating a pre-rollback backup. Manual/risky/system-level items stay
  fail-closed.
- **Security audit** (`scripts/ccc-security-audit.sh`, `/security-audit`) — read-only,
  metadata-only checks for sensitive file permissions, settings allowlist posture, scanner
  integrity, and push-spool/cache redaction risk. It never prints matched secret text or file
  contents; `--fix` is reserved for a later repair slice.
- **Headless runner** (`headless.sh`) — `claude -p` wrapper for cron/A2A/CI with JSON output
  and a read-only tool baseline; guard enforcement still applies.
- **Agent-cron store/list/due/lock/run** (`scripts/agent-cron.sh`, `/agent-cron`) — first-class
  durable task-definition surface for future headless cron work. The current slices validate/list
  `~/.claude/state/agent-cron/tasks.json` against `schemas/agent-cron-task-store.schema.json`,
  provide a read-only `due` resolver for schedule/catch-up planning, expose local atomic
  task-lock `acquire`/`release`/`probe` primitives with stale-lock reporting, and support
  `run <task-id> --dry-run` execution-plan previews plus explicit manual `run <task-id>`
  execution for due enabled tasks. Manual run acquires/releases the local lock, invokes
  `headless.sh`, appends bounded `runHistory`, and updates
  `lastRunAt`/`lastStatus`/`lastRunId`. Failed runs can persist bounded
  `retryState`/`retryEligibleAt` according to optional `retryPolicy`; this is
  planning state only. `scheduler --dry-run` adds a read-only single-tick
  scheduler plan that reports `would-run`/`skip` actions without acquiring
  locks, executing prompts, writing spool files, or installing timers.
  `scheduler --execute` is an explicit one-shot executor for approved live/systemd
  use; it consumes due/retry-due tasks through the same locked manual run path
  but still never installs timers itself. `scripts/install-agent-cron-systemd.sh`
  installs the systemd service/timer only when called with `--apply`; default is
  dry-run. When
  `notify=telegram-owner`, manual run writes a short redacted owner-only bridge spool entry
  (`CCC_AGENT_CRON_PUSH_SPOOL`/`CCC_PUSH_SPOOL`), but still does not directly call
  Telegram/provider APIs or install schedulers.
- **A2A Claude Code worker lane** — documentation for nodes whose broker poller keeps the
  historical `a2a-hermes-worker` service name while the task analysis adapter is switched to
  `claude-a2a-analysis-bridge` and broker metadata reports `runtime=claude-code`. The
  `claude/agents/a2a-*.md` roster carries advisory `model_tier` metadata (read-only
  explorer/researcher = `low-cost`, implementer/verifier = `upper`) and requires cost/token
  notes when runner accounting is available. Mobile nodes can preflight the native
  Termux/glibc-runner worker path with `scripts/a2a-termux-native-worker.sh`, using
  `docs/examples/a2a-termux-native-worker.env.example` as the non-secret env template. See
  [`docs/a2a-claude-worker.md`](docs/a2a-claude-worker.md).
- **Session Distiller** (`claude/hooks/distill.sh`, `/distill`) — PreCompact/SessionEnd hook
  pipeline (0.3.15+) that distills live transcripts via `claude -p --model haiku` (inherits
  parent OAuth, no API key) and routes results to **Honcho** (auto-push of working/relational
  facts) + a **human-gated wiki-candidates queue** (`~/.claude/state/wiki-candidates.md`).
  Includes a retry drain worker (`queue-drain.sh`, 0.3.18) for failed Honcho pushes, a
  **local-facts writer** (`local-facts.sh`) that appends distilled facts to
  `~/.claude/state/memory-facts.jsonl` so the hot SQLite index can recall them next session
  (no network, independent of Honcho), and an
  off-switch (`distill.disabled`) / dry-run toggle. Fleet verification: see issue #82 and
  `scripts/ccc-distill-check.sh --json` for per-node health snapshot. Non-root installs: set
  `CCC_STATE_DIR` (state/log/queue), `CLAUDE_PROJECTS_DIR` (transcript discovery), and
  `CCC_NODE` (node label); the checker respects `CCC_STATE_DIR`.
- **Skill Review** (`claude/hooks/skill-review.sh`, `/skill-suggest`) — Hermes-style
  SessionEnd self-improvement pass that reviews recent transcripts via `claude -p --model haiku`
  and stages reusable `SKILL.md` drafts under `~/.claude/state/pending-skills/`. It never writes
  directly to `~/.claude/skills`; install/overwrite requires explicit human approval through the
  `skill-suggest` workflow. Use `skill-review.disabled` as the off-switch and
  `CCC_SKILL_REVIEW_COOLDOWN_SECONDS` to tune cost cadence.

## Quick start

```bash
git clone https://github.com/jinwon-int/ccc-node.git
cd ccc-node
./setup.sh --dry-run   # preview
./setup.sh             # install into ~/.claude and seed ~/.hermes templates

# Telegram bridge (optional, run from the bridge/ subdir):
cd bridge && ./start.sh --path /root -d   # daemon-supervised start
```

Then complete the checklist `setup.sh` prints (fill placeholders, set `honcho.json`,
install `wiki-agent`, `gh auth login`, start a fresh session to verify injection).

### Install the portable surface as a plugin (optional)

The node-agnostic surface — enforcement guard + observability hooks, A2A agents, skills,
and slash commands — is also packaged as a Claude Code plugin. The marketplace catalog
lives at `.claude-plugin/marketplace.json` and points the plugin root at the existing
`claude/` tree (`source: "./claude"`), so components are auto-discovered with no duplication:

```bash
/plugin marketplace add jinwon-int/ccc-node
/plugin install ccc-node@ccc-node
```

The plugin and `setup.sh` are complementary: the **plugin** carries the portable surface,
while **`setup.sh`** installs the node-local memory bootstrap (SessionStart/PostCompact
injection, working-state checkpoint) that is inherently node-specific. See `CHANGELOG.md`.

**Avoiding double-firing.** The portable enforcement/observability hooks (guard/audit/redact/
notify) must have exactly one owner — `settings.json` and the plugin both register hooks and
Claude Code does not de-duplicate them. So `setup.sh` composes `settings.json` from two sources:
`claude/settings.base.json` (node-local hooks + statusLine + outputStyle, always installed) and
`claude/hooks/enforcement-overlay.json` (the portable hooks). Pick one mode per node:

- **Standalone** (default): `./setup.sh` merges base + overlay — `settings.json` owns everything,
  no plugin required.
- **Plugin mode**: `./setup.sh --with-plugin` installs lean settings (base only); the installed
  **plugin** owns the portable hooks. Use this on nodes that consume ccc-node via the marketplace.

Don't enable the plugin on a node installed standalone (or vice-versa) — that double-fires the
portable hooks. The validator asserts the overlay and the plugin's `hooks/hooks.json` stay
equivalent so the two modes enforce identically.

## Layout

```
claude/
  settings.base.json       # node-local hooks + statusLine + outputStyle (always installed)
  hooks/enforcement-overlay.json  # portable hooks; merged in for standalone, omitted in plugin mode
  settings.local.json      # local permission allowlist
  CLAUDE.md.template        # operating policy w/ <PLACEHOLDERS> -> ~/.claude/CLAUDE.md
  hooks/
    load-memory.sh         # bounded snapshot injector (no-network startup; local hot cache optional)
    refresh-memory.sh      # parallel Wiki+Honcho cache refresh + per-source metadata
hermes/
  memories/MEMORY.template.md   # -> ~/.hermes/memories/MEMORY.md
  memories/USER.template.md     # -> ~/.hermes/memories/USER.md
  honcho.template.json          # -> ~/.hermes/honcho.json (set baseUrl/peer/target)
bridge/                    # Telegram <-> Claude Code bridge (vendored, history preserved)
  start.sh                 # daemon/supervisor entry (--path / --stop / --status / -d)
  core/                    # bridge source (upstream-independent fork): bot.py orchestrator +
                           #   focused helpers ui/media/paths/task_queue/revert/sdk_text (unit-tested)
  interaction/ utils/ ...  # bridge source (upstream-independent fork)
docs/
  a2a-claude-worker.md     # A2A poller-vs-analysis-backend wiring + verification
  examples/a2a-termux-native-worker.env.example  # non-secret native mobile worker env shape
scripts/
  a2a-termux-native-worker.sh  # validates/execs native Termux Node worker.js env (PR-first)
  ccc-memory-check.sh          # read-only Wiki/Honcho/local-index cache health snapshot
  ccc-memory-index.sh          # build/update SQLite FTS5 local hot-memory index
  ccc-memory-search.sh         # query local hot-memory index as JSON
  ccc-memory-query.sh          # build redacted task-aware local/remote memory queries
  ccc-memory-explain.sh        # explain effective query, cache staleness, budgets, and ranked recall
  ccc-memory-eval.sh           # no-network smoke + golden/scenario memory quality harness
  ccc-memory-benchmark-export.sh # synthetic benchmark fixture export (no real memory by default)
  ccc-wiki-triage.sh           # local human-gated Wiki candidate review/marking
  validate-harness.sh          # CI harness validation, including forbidden context-file guard
setup.sh                   # idempotent bootstrap (won't overwrite existing real files)
.gitignore                 # blocks credentials, live memory, caches, sessions
```

## Secret-handling policy (important)

This repo follows the Seoyoon rule: **never store raw secrets — only locations / handling rules.**
The following are intentionally absent and must be provided per node:

| Item | Where it comes from |
|---|---|
| `~/.claude/.credentials.json` (Claude OAuth) | created by `claude` login |
| GitHub token | `gh auth login` (node-local; never committed) |
| Honcho endpoint `baseUrl` | set in `~/.hermes/honcho.json` (gitignored) |
| Real `MEMORY.md` / `USER.md` content | written per node from the templates |

`.gitignore` blocks `.credentials.json`, `honcho.json`, real `MEMORY.md`/`USER.md`,
hook caches, sessions, and anything matching `*token*` / `*secret*`.

## Dependencies a node needs

- [`jinwon-int/wiki-agent`](https://github.com/jinwon-int/wiki-agent) installed at
  `${CCC_WIKI_AGENT_BIN:-$HOME/.wiki-agent/bin/wiki-agent}` (Family Wiki reads/prefetch).
  Existing root VPS installs still resolve to `/root/.wiki-agent/bin/wiki-agent`.
- `jq`, `curl`, `flock` (standard).
- A Honcho endpoint reachable from the node (set in `${CCC_HERMES_DIR:-$HOME/.hermes}/honcho.json`).

## Non-root path overrides

`setup.sh` defaults to the historical root-compatible layout through `$HOME`, but
non-root nodes can make the target paths explicit without changing existing root
behavior:

| Variable | Default | Purpose |
|---|---|---|
| `CCC_CLAUDE_DIR` | `$HOME/.claude` | Claude Code harness, hooks, memories, output styles, commands, skills |
| `CCC_HERMES_DIR` | `$HOME/.hermes` | `honcho.json` and Hermes-side local config templates |
| `CCC_WIKI_AGENT_BIN` | `$HOME/.wiki-agent/bin/wiki-agent` | Checklist path for the Family Wiki reader/writer binary |
| `CCC_BRIDGE_DEFAULT_PATH` | `$HOME` | Suggested Telegram bridge `--path` workspace in the printed checklist |
| `CCC_MEMORY_PROFILE` | `honcho` | Memory profile: `honcho`, `hybrid`, or `max-perf` |
| `CCC_MEMORY_CACHE_DIR` | `$CCC_CLAUDE_DIR/hooks/cache` | Wiki/Honcho cache and refresh metadata location |
| `CCC_STATE_DIR` | `$CCC_CLAUDE_DIR/state` | Node state plus local `memory-index.sqlite` |
| `CCC_HONCHO_MEMORY_ENABLED` | `1` | Set `0`/`false`/`off` to remove Honcho from the read path while keeping local/Wiki memory |
| `CCC_LOCAL_MEMORY_ENABLED` | `1` (on) | Local hot-memory index search is queried for every profile by default; set `0`/`false`/`off` to opt out |
| `CCC_MEMORY_INJECT_DEDUP` | `1` (on) | Cross-source injection dedup: drops a local hot-memory hit when its content is already injected verbatim by the MEMORY/wiki/honcho blocks (lossless — content truncated out of those blocks is kept; distilled facts always kept), so the budget isn't double-spent. Set `0`/`false`/`off` to disable |
| `CCC_MEMORY_FUSION` | `1` (on) | Fuse the lexical lane with a stdlib char-ngram fuzzy lane (RRF) so typo/transposed/morphological queries still recall; set `0`/`false`/`off` for the lexical lane only |
| `CCC_MEMORY_EMBED_CMD` | unset (off) | Opt-in semantic lane: a command that reads text on stdin and prints a JSON float array (the repo ships no provider/key). Doc vectors are precomputed during background refresh; only the query is embedded at search time (tight timeout, fail-open). See `docs/examples/memory-embed-openai.example.sh`. Unset → no embedding, no startup network |
| `CCC_MEMORY_EMBED_MODEL` / `CCC_MEMORY_EMBED_TIMEOUT` / `CCC_MEMORY_EMBED_MIN_SIM` | `` / `15` / `0.55` | Embedding model label, per-call timeout (s), and minimum cosine for the semantic lane |
| `CCC_MEMORY_VOLATILE_TTL_DAYS` | `14` | Decay/forgetting: structured facts marked `volatile` (e.g. distilled `task-progress`) older than this are dropped at index time so stale working state stops surfacing. Durable and undated facts never decay; set `0` to disable decay entirely |
| `CCC_MEMORY_MAX_BYTES` | `12000` | Total SessionStart memory injection byte budget |
| `CCC_MEMORY_QUERY_MAX_BYTES` | mode-specific | Max task-aware query bytes; remote defaults lower than local |
| `CCC_WIKI_CACHE_MAX_AGE_SEC` / `CCC_HONCHO_CACHE_MAX_AGE_SEC` | `CCC_MEMORY_CACHE_TTL_SEC` | Per-source stale-warning thresholds |
| `CCC_MEMORY_EVAL_MODE=golden` or `ccc-memory-eval.sh --golden` | off | Run deterministic no-network precision/recall/MRR/p50/p95 memory quality benchmark |

Example non-root preview:

```bash
HOME=/home/ccc \
CCC_CLAUDE_DIR=/home/ccc/.claude \
CCC_HERMES_DIR=/home/ccc/.hermes \
CCC_WIKI_AGENT_BIN=/home/ccc/.wiki-agent/bin/wiki-agent \
CCC_BRIDGE_DEFAULT_PATH=/home/ccc \
./setup.sh --dry-run
```

The setup script prints the resolved paths and never prints or moves raw secrets.

On **recent Android (Android 16 / Samsung S23 Ultra class)** the Claude Code CLI
itself has a device-level blocker (glibc-native binary won't link under Termux) —
use the JS-pinned, non-proot path. See
[docs/android-termux-claude.md](docs/android-termux-claude.md).

## Public source visibility boundary

This repository is being prepared for possible public source visibility. A
public repository setting would be source-only: it would not approve release or
tag creation, package/image publication, production deploy/restart/reload,
database mutation, provider or Telegram sends, credential movement, history
rewrite, or any other live operation.

Runtime credentials and private operational data must stay outside the
repository. Example configuration must use placeholders only.
