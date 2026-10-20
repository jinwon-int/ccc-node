# Doctor diagnostics

`ccc-doctor.sh` is the read-only harness drift report and conservative repair entry point.

## What it checks

- `settings.json` JSON validity and install mode (`standalone`, `plugin`, or ambiguous).
- `outputStyle` and `statusLine` wiring.
- SessionStart/PostCompact memory bootstrap hooks and portable enforcement hook wiring.
- Installed hook scripts and output-style files under the target Claude directory.
- Telegram bridge status output when `bridge/start.sh` is present.
- **Bridge boot path**: the systemd unit that would restart the bridge must point at the
  checkout the bridge is *actually* serving from. A node can hold several checkouts
  (`/opt/ccc-node`, `/root/ccc-node`, `/home/<user>/ccc-node`), so the live checkout is
  derived from the running process — never from whichever path is found on disk first.
  A mismatch is `수동필요`: the running bridge looks healthy, so nothing surfaces until a
  reboot or `systemctl start` serves the stale twin. Observed on yukson 2026-07-27, where
  an `enabled` unit pointed at a checkout 111 commits behind the live one.
- Harness version anchor via `scripts/ccc-version.sh`.
- **Cron generation drift** (`#1081`): installer-managed cron entries carry a
  `gen=h_<sha256:12>` stamp of the rendering installer's content. Entries stamped by
  older code — or unstamped pre-`#1081` lines — are `경고` (non-fatal); re-apply the
  named installer to re-render. See *Cron generation drift* below.
- Selected agent provider. For `CCC_AGENT_PROVIDER=codex`, deterministic CLI, app-server surface, and login readiness without a model turn or Telegram access.
- **Always-on budget** (`#2384`): always-on injection per source (chars/bytes, share of
  the provider cap); over cap is `경고`, or `수동필요` under `--strict`. See *Always-on budget* below.

The report is Markdown and classifies rows as:

| Class | Meaning |
|---|---|
| `정상` | Matches the expected harness state. |
| `경고` | Non-fatal drift or optional component needs attention. |
| `교정가능` | Doctor can repair it in an explicitly approved scoped apply. |
| `수동필요` | Manual/operator action required; doctor refuses automatic repair. |

## Commands

```bash
scripts/ccc-doctor.sh
scripts/ccc-doctor.sh --json                 # one JSON object
scripts/ccc-doctor.sh --strict               # over-cap always-on budget fails (exit 1)
scripts/ccc-doctor.sh --always-on-summary    # one-line always-on budget only
scripts/ccc-doctor.sh --fix                  # dry-run only
scripts/ccc-doctor.sh --fix --apply          # settings-only repair after backup
scripts/ccc-doctor.sh --fix --apply --scope=files
scripts/ccc-doctor.sh --rollback             # dry-run only
scripts/ccc-doctor.sh --rollback --apply
```

## Repair boundary

- `--fix` and `--rollback` are dry-run by default.
- `--fix --apply` defaults to `--scope=settings` and writes only deterministic `settings.json` repairs from canonical repo templates after creating a backup tar.
- `--fix --apply --scope=files` reinstalls only allowlisted hook/output-style files from `claude/` after a scoped backup.
- File repair refuses symlinks, path traversal, missing repo sources, plugin/standalone double-firing risk, and unsupported targets.
- `--rollback --apply` restores only `settings.json` from the latest doctor backup after creating a pre-rollback backup.
- It never touches remote nodes, secrets, broker/Gateway restarts, bridge restarts, migrations, provider sends, or DB/ACK/replay state.

## Provider readiness

`CCC_AGENT_PROVIDER` accepts `claude` (the unchanged default) or `codex`. Set
`CCC_CODEX_CLI_PATH` to the Codex executable path or command name when the
default `codex` lookup is not suitable.

For Codex, doctor resolves an executable and runs bounded, non-mutating probes:

- `--version` must return recognizable Codex version output.
- `app-server --help` must expose the app-server surface used by the bridge.
- `login status` must report an authenticated CLI session.

The probes never invoke a model turn or Telegram. Their raw stdout/stderr,
command payloads, executable/credential paths, auth JSON, tokens, and account
identity are never included in diagnostics. Missing/non-executable binaries,
timeouts, unauthenticated or malformed output, and probe exceptions produce
stable redacted `수동필요` rows, `readiness: failed`, and a nonzero exit.

On a Codex node, doctor also diagnoses the **repo-shipped managed Codex skills**
(#647) via the read-only `ccc_codex_skills.py plan` contract, body-free:

- Missing or outdated managed skills are `교정가능` — reinstall with `setup.sh`.
- A drifted managed skill or a user skill that name-collides with a managed one
  is `교정가능` (drift restore / rename), never a silent overwrite.
- An unsafe `CODEX_HOME`/skill layout (not owner-only `0700`, or a symlink) is a
  `수동필요` blocker with a fix action.

These findings are **independent of readiness**: dormant Claude-only harness
assets (outputStyle/statusLine/hook/overlay drift) on a Codex node stay
`교정가능`/`정상` and never block the Codex readiness verdict, and unprovisioned
managed skills are correctable rather than a readiness failure.

Human output adds `provider` and `readiness` headers. `--json` carries the same
diagnostic information with additive `provider`, `readiness`, `counts`, and
`rows` fields.

Before a Codex rollout, install/authenticate the CLI, set the provider variables,
and require a ready doctor result. Codex approval requests are owner-only and
turn-scoped: Allow or Deny each request; there is no **Allow All**. Stop the old
bridge before starting the new one because two services must never poll the same
Telegram bot token concurrently. Roll back by stopping Codex, restoring
`CCC_AGENT_PROVIDER=claude`, and starting Claude as the sole poller.

## Cron generation drift

Installer-rendered cron entries freeze at apply time: nothing re-runs the
installer when the repo moves, so a fix like `#996` (piri CLI pin) never reaches
nodes until someone re-applies by hand. Since `#1081` each managed line carries
`gen=h_<sha256:12>`, a content-only hash of the installer that rendered it
(`scripts/lib/installer-gen-stamp.sh`). Doctor recomputes the stamp from the
current checkout and compares — it never re-renders the line, because apply-time
flags (e.g. nunchi's `--audience-scoped`) are unknowable at check time and a
re-rendered comparison would false-positive on exactly those configurations.

One row per known marker (`cron gen memory-refresh`, `cron gen pr-status-poll`,
`cron gen skill-autosave`, `cron gen nunchi`):

| State | Class | Meaning |
|---|---|---|
| absent | `정상` | lane not opted in |
| all gens match | `정상` | entries rendered by the current checkout |
| any gen mismatch | `경고` | entry frozen at older installer — re-apply with the printed hint |
| any line unstamped | `경고` | pre-`#1081` entry — re-apply once to stamp it |
| stamp not computable | `경고` | checkout missing the installer or the gen-stamp lib |

All rows are non-fatal: they never change the exit code. `install-nunchi.sh`
re-applies need the node's original provider/audience flags.

Marked lines no repo installer renders are classified separately as
`cron unmanaged markers`: documented hand-installed lines are `정상` with
their labels; unknown markers are `경고` so stale duplicates like the `#1079`
ghost entries stay visible. The check reads only the crontab of the user
running doctor.

Documented hand-installed markers (`CRON_KNOWN_UNMANAGED_MARKERS`):

| marker | what it runs | where |
|---|---|---|
| `# ccc-node:self-update` | `ccc-self-update.sh run` (docs/self-update.md) | every node |
| `# ccc-node:live-backups-rotate` | `ccc-live-backups-rotate.sh` | every node |
| `# ccc-node:skill-promotion-collect` | `ccc-skill-promotion.py collect` nightly | hub (seoseo) |
| `# ccc-node:skill-promotion-drop-report` | `ccc-skill-promotion.py drop-report` weekly | hub (seoseo) |
| `# ccc-node:gate-sim` | `skill-review/gate-sim.sh` nightly simulation | opt-in |
| `# ccc-node:kimi-mail-monitor` | operator-owned node-local mail monitor | daegyo |
| `# ccc-node:wiki-log-rotation-reminder` | operator-owned reminder script | yukson |

A marker in this table is a *known lane without an installer*: the line is
expected, but nothing re-renders it and it never carries a gen stamp. Promote a
lane to an installer (and `CRON_MARKER_INSTALLERS`) once its command shape has
to change fleet-wide — that is what happened to `fleet-skills-sync` in #1398.

## self-update stall

One `self-update` row, read from `$CCC_STATE_DIR/self-update.log` (falling back
to `<claude-dir>/state`). The verdict is the **last terminal record**, not the
presence of any abort in history:

| State | Class | Meaning |
|---|---|---|
| log absent | `정상` | node does not run self-update |
| no terminal record in the log tail | `정상` | nothing to judge |
| last record is `done`/audit JSON | `정상` | last attempt reached an end state |
| `wrong-branch`, `dirty-tree`, `no-repo` | `수동필요` | node receives no harness updates until a human restores the checkout. Note: a `wrong-branch` stall whose stray branch is fully pushed with a clean tree now self-recovers at the next tick (#1328), so it only reaches this row when recovery is impossible (unpushed commits, dirty tree, `main` held elsewhere) or was disabled with `CCC_SELF_UPDATE_AUTO_RECOVER=0` |
| any other abort (e.g. `fetch-failed`) | `경고` | transient; the next tick retries |

`consecutive=N` counts the unbroken run of the newest reason only — a different
earlier reason is a separate incident, and inflating the streak would misreport
how long the current one has persisted. A repaired node reads `수동필요` until
its next successful tick, which is the honest reading: no successful update has
happened since the failure.

Why this exists as a pull-based check even though #1060 already alerts: the
alert is a one-shot event. After #1061 documented the worktree discipline the
same stall still recurred on two nodes (#1328), because nothing surfaced a node
that had quietly stopped updating days earlier.

## Stale memory guidance

One `stale-memory-guidance` row (#2238). `setup.sh` seeds the node-owned
`~/.claude/CLAUDE.md`, `~/.claude/memories/MEMORY.md` (and Hermes'
`~/.hermes/memories/MEMORY.md`) only when they are absent, so a template fix
never reaches an already-seeded node. After the Honcho retirement (2026-09-01,
#1436) nine nodes still told every session to recall from Honcho.

Each file is scanned per sentence for the old template shapes: `+ Honcho +`
stack lists, `cached Honcho`, the `Honcho — working` heading, the Honcho recall
endpoint, `honcho.json`, `→ Honcho` routing, a "current stack" naming
Honcho/Supermemory, and a reads row naming them. A sentence that also says
retired / legacy / deprecated / removed / 퇴역 / 폐기 / "do not query" is the fix,
not drift, so the current template line about a missing `honcho.json` stays quiet.

| State | Class |
|---|---|
| none of the files exist | `정상` (해당 없음) |
| no stale sentence | `정상` |
| stale sentences | `경고` — file, line numbers and pattern labels (never file text) |

For CLAUDE.md the row also reports `memory-span=template|differs|absent`: whether
the span from `## Memory Stack` up to `## Fresh Approval Required` still equals
the repo template (the `ccc:memory-stack` marker comment lines are ignored, so a
refreshed file and a hand-fixed one both read `template`). `differs` alone is
informational (nodes may customize it). `CCC_DOCTOR_HERMES_HOME` overrides the
Hermes home (default: the sibling `.hermes` of the Claude dir). The patterns
live in `scripts/ccc_memory_guidance.py`, shared with the refresh below.

The doctor never rewrites these files. Remedy:

```bash
./setup.sh --refresh-guidance --dry-run   # preview: per file, what would change
./setup.sh --refresh-guidance             # back up + rewrite managed guidance only
```

`--refresh-guidance` is a standalone mode: it installs nothing else, exits
after the three files above, and only `--dry-run` combines with it. Per file it
writes a `<file>.bak-refresh-guidance-<UTC timestamp>` backup first, then:

| Shape | Action |
|---|---|
| CLAUDE.md with `<!-- ccc:memory-stack:begin -->` … `<!-- ccc:memory-stack:end -->` | replace that block with the template's block |
| CLAUDE.md without markers, span stale **and** equal to a known old template version | replace `## Memory Stack` … (not including) `## Fresh Approval Required` with the marked template block |
| CLAUDE.md without markers, span already the current template text | no-op |
| CLAUDE.md without markers, any other span (node-customized) / unbalanced markers | `refused` — file untouched |
| A line equal to a known old template line (Standing Orders reads row, MEMORY.md memory-stack line) | replace with the current template line |
| MEMORY.md stale lines that are not an exact known line | `refused` — file untouched |

Comparison ignores trailing spaces and blank-line runs only. Everything outside
the managed block / known lines — every node-specific section — is preserved,
and a second run is a no-op (`current`, no new backup). Stale lines left after
the refresh are listed as `still stale, fix by hand`. Exit 0 when every present
file is current/refreshed and clean, 1 when anything needs a hand fix, 2 when
the repo template lacks its markers. The report names files, line numbers and
pattern labels only. A symlinked MEMORY.md is rewritten once, at its target.

## Push spool dwell

One `push spool dwell` row (#2223). Push consumers — the Telegram
`PushNotifier` and the Matrix `MatrixSpoolNotifier` — poll their spool every
few seconds, so a `*.json` record still pending after the threshold means no
running process drains that directory. Writers cannot see this (`delivery:
spooled` is their success), and the bridge health probe only counts the
directory its own process consumes.

Directories checked, deduplicated by resolved path (a symlinked orphan counts once):

- the writers' default `$CCC_STATE_DIR/telegram-spool` (or this process's `CCC_PUSH_SPOOL`), marked `writer-default`;
- every `*-spool` dir in the doctor's state dir and in other accounts' state dirs it can read (`/root/.claude/state`, `/home/*/.claude/state`) — writer lanes of a different account, e.g. a root crontab next to a user-lane bridge;
- `CCC_PUSH_CONSUME_SPOOL` / `CCC_PUSH_MIRROR_DIRS` from this process, and each spool's subdirs except `sent/` (fan-out mirrors).

| State | Class |
|---|---|
| no spool dir | `정상` (해당 없음) |
| no record older than the threshold | `정상` |
| records older than the threshold | `경고` — lists up to five dirs, oldest first, with count and age |

Only file names and modification times are read; record bodies and other
processes' environments are not. `CCC_DOCTOR_PUSH_SPOOL_DWELL_MINUTES` sets the
threshold (default 30, `0` disables the row); `CCC_DOCTOR_PUSH_STATE_ROOTS`
(`os.pathsep`-separated, globs allowed) replaces the extra state dirs.
The usual remedy is to point the remaining consumer at the writers' directory, or
to symlink the orphan directory to the consumed one, and to move the stale records into `sent/`
rather than delivering day-old notices.

## Always-on budget

`always-on: <source>` rows plus one `always-on budget` verdict row (#2384).
Always-on text is what the provider loads into every session before the task
says anything; it is sized per source, in chars and UTF-8 bytes, with each
source's share of the provider cap.

| Provider | Sources | Cap (chars) |
|---|---|---|
| `claude` | `~/.claude/CLAUDE.md` (global only; a project CLAUDE.md depends on cwd), the active output style (`outputStyle` → `output-styles/<name>.md`, else a frontmatter `name:` match), and every known SessionStart hook: `load-tools.sh` (`hooks/tools-cheatsheet.md`), `load-memory.sh` (memory snapshot), `nunchi/sessionstart.sh` (nunchi working memory), `report-style-canary.sh` | 24,000 |
| `codex` | the first non-empty `$CODEX_HOME/AGENTS.override.md`, else `AGENTS.md`, with the ccc memory materializer's marked block split out as `memory snapshot (managed block)` | 8,000 |
| other | not measured (`정상`, `not-measured`) | — |

The caps are harness2's draft values; `CCC_ALWAYS_ON_MAX_CHARS` (a positive
integer) overrides them on one node.

SessionStart hooks are read from `settings.json` and `settings.local.json`
together, the way Claude Code merges them (`scripts/install-nunchi.sh`
registers the nunchi hook in `settings.local.json`); `outputStyle` in
`settings.local.json` wins. Any other SessionStart hook whose stdout reaches
the context is listed as `unmeasured hook <basename>` — visible, not counted.
Hooks with stdout sent to `/dev/null` or backgrounded (`pending-drain.sh`)
are skipped.

The memory snapshot row is `estimated`. Doctor never runs `load-memory.sh` —
it spawns a detached cache refresh and a query-time local search — so it
rebuilds the deterministic parts the hook reads (MEMORY/USER with the Hermes
fallback, PERSONA, the resume pointer, the Family Wiki cache), each under the
hook's own byte cap (`CCC_BUILTIN_MEMORY_MAX_BYTES`, `CCC_RESUME_MAX_BYTES`,
`CCC_WIKI_MAX_BYTES`), then the `CCC_MEMORY_MAX_BYTES` total. Query-time local
hot memory and section headers are not counted.

The nunchi row is not executed either, and nothing writes the hook's output to
disk. It reports the byte ceiling of `nunchi.py assemble` —
`max(CCC_NUNCHI_ASSEMBLE_BUDGET (3000), CCC_NUNCHI_CONSTRAINT_BUDGET (12000))`
— converted to chars with `snapshot.md`'s char/byte ratio, labelled
`estimated (budget ceiling)`. With `CCC_NUNCHI_ASSEMBLE=0`, the hook injects
`head -c 3000 snapshot.md`, which is read as `cached`. The row is `inactive` when
nunchi mode is off, the runtime is audience-scoped, or `snapshot.md` is missing.
The report-style canary is `conditional`: it is counted only while
`state/report-style-canary.flag` is armed. Its text is computed with the hook's own
reader.

| State | Class |
|---|---|
| total at or under the cap | `정상` |
| total over the cap | `경고`; the action names the sources to cut, largest first, and by how many chars (`cut 1,000 chars, largest first: CLAUDE.md -1,000 (of 20,000; …)`) |
| total over the cap under `--strict` | `수동필요` (exit 1) |

A missing source is `absent`, an unwired hook `not-wired`, and a built-in or
missing output style `no-file`; none of these is a finding.
`--always-on-summary` prints only the one-line summary, which `/node-status`
shows. Before adding always-on text, see the checklist in
[`harness.md`](harness.md#always-on-text-budget).

## Fleet matrix

`ccc-doctor-fleet-matrix.sh` summarizes already-collected doctor output; it does not SSH or mutate nodes.

```bash
bash scripts/ccc-doctor-fleet-matrix.sh --evidence doctor.txt --node-list dungae,nosuk,soonwook --json
```

Input blocks look like:

```text
===== nosuk =====
# ccc doctor
- harness version: `v0.4.0-2-gabc1234`
...
```

The JSON output includes per-node `version`, `status`, `reason`, evidence presence, and summary counts.
