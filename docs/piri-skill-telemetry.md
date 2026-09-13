# Piri skill telemetry (#1692 option B)

`piri/extensions/skill-usage.ts` adapts Piri extension events to the existing
`claude/hooks/skill-usage-log.sh` stdin contract. This supplies the missing Piri
collection path; it does not deploy the extension or complete the parent issue's
observation and rollout conditions. Skill directory linking (A) and the redundant
index default-off change (C) are separate, completed work.

## Evidence semantics

| Piri event | Existing ledger tool | Evidence |
|---|---|---|
| Paired `tool_call` and `tool_execution_end` for `read`, skill document path, and exactly `isError: false` | `Read` | A successful read tool result for that document, possibly a partial read. This does not prove the model followed the skill. |
| `input` starting `/skill:<name>`, matching `getCommands()` with `source: skill` | `Skill` | An explicit invocation **attempt**, not proof of expansion or a successful document load. |

Piri emits `input` before skill expansion. Later handlers may transform or handle
it, expansion may fail, and command availability alone does not establish runtime
settings or execution success. A listed skill without an invocation produces no
line. Failed, unpaired, duplicate completed, or malformed read results produce no
line. Piri treats `#` as part of a filename: a successful read of
`SKILL.md#other-file` is excluded; only the exact `SKILL.md` filename counts.
In a no-tools session only explicit attempts can be observed. SDK paths
that bypass these events are outside this coverage.

The existing ledger contains only `ts`, `skill`, and `tool`. It cannot distinguish
Claude from Piri, and `skill-usage-log.sh report` aggregates both evidence types.
Do not present its total as a count of successful Piri reads or use zero as proof
that a skill is unused. A request followed by a read is two distinct observations.
Startup discovery and context injection are deliberately not counted.

## Bounded logger adapter

`pi.exec()` cannot pass stdin. The extension instead starts
`bash <logger> log` with a private process group and a piped JSON payload.
Successful reads pass a synthetic `/skills/<name>/SKILL.md` path; the original
path, request arguments, prompt and tool-result content are never forwarded.
Names must survive the existing logger's lowercase/digit/hyphen format without
loss; unsupported names are dropped rather than silently renamed.

At most four logger children run concurrently; overflow is dropped without a
queue. Each has a default four-second deadline, configurable through
`CCC_SKILL_USAGE_LOG_TIMEOUT_MS` and clamped to 50–30,000 ms. Timeout kills the
entire group, including blocked `flock` or background descendants. The foreground
event handler never awaits the child. Pending read tracking is capped at 256,
with bounded identifiers and paths; old pending observations may be dropped.

The shared logger retains its existing blocking lock, 64 KiB input guard and
owner-only ledger format. It has **no retention policy**. Its successful exit is
best-effort and does not itself prove a line was written. This adapter neither
adds a separate writer nor changes existing ledger storage protections.

Logger lookup occurs at event time: `CCC_SKILL_USAGE_LOGGER`, then
`$CCC_CLAUDE_DIR/hooks/skill-usage-log.sh`, then
`$HOME/.claude/hooks/skill-usage-log.sh`. A missing explicit override disables the
adapter and prints one fixed, path-free stderr note per extension instance.
Other missing logger or subprocess failures remain best-effort. Logger symlinks
are rejected. No extension event scans session files or contacts a provider.

## Managed installation

A future `setup.sh` run installs only if the resolved Piri agent directory
already exists: `PIRI_CODING_AGENT_DIR`, otherwise `$HOME/.piri/agent`.
The destination is `extensions/skill-usage.ts`; tests never ship. Installation
uses the existing atomic file primitive beneath a narrow extension ownership
check. Same-name untracked files and tracked files differing from the prior
manifest digest are preserved. Preserving a conflicting file means this managed
extension may remain unavailable or outdated on that node.

`state/repo-extensions.manifest` records ownership digests. Invalid entries,
traversal, symlink components/targets and concurrent extension installs are
rejected before extension changes. Replaced or retired pristine managed files
are archived under private `state/retired-extensions.*` directories outside the
extension loader, and manifest/archive files use mode 0600. A node-edited file
keeps its prior digest. The manifest is replaced atomically, including an empty
set; it is not a claim of whole-setup transactional rollback for Piri assets.
If a later setup step fails, inspect the manifest and archives before retry or
restoration; do not delete operator files to force ownership.

## Validation and remaining acceptance

`bash piri/extensions/skill-usage.test.sh` drives mocked Piri events and temporary
logger fixtures, including the real repository logger in a private fixture HOME.
It requires Node >=22.6 for TypeScript stripping and fails if that capability is
missing. `bash scripts/setup.test.sh` exercises fixture installations, ownership,
archive, symlink, traversal and lock behavior. No test loads a production Piri
extension or calls a model. The harness discovers both suites from tracked files.

No fleet deployment, restart or live provider invocation accompanies this source
change. Activation requires a later approved setup and a new Piri session. A
per-node observation must distinguish successful read evidence from explicit
attempts and verify actual ledger writes before culling or usage conclusions.
Until then, Piri zero counts remain unmeasured. The monthly audit procedure
([skill-registry.md](skill-registry.md)) is unchanged.
