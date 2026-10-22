# Skill usage report and two-stage curation (#2239)

`scripts/ccc-skill-usage-report.py` is a read-only report over the two skill
usage ledgers each node keeps. It feeds the curation decisions in #1739
(archive candidates) and the graduation checklist in
[`skill-graduation.md`](skill-graduation.md) (real usage on the fleet). It
never moves, deletes or writes anything, and its output carries counters,
timestamps and classification labels only. It never includes SKILL.md bodies
or descriptions.

## Usage sources

The report reads the same ledgers as the curator
(`claude/hooks/skill-review/curator.py`), and it imports the curator's readers
so the two cannot disagree about what "unused" means:

| Ledger | Writer | Records |
|---|---|---|
| `state/skill-autosave-usage.json` | `curator-bump.sh` (PostToolUse `Skill`) | per-skill `use_count`, `last_used_at`, curator `state`, `stale_marked_at` |
| `state/skill-usage/usage.jsonl` | `skill-usage-log.sh` (PostToolUse `Read`/`Skill`), codex/piri/danso loggers | one `{"ts","skill","tool"[,"runtime"]}` row per load |

Every `Skill` call is recorded in both ledgers, so the reported `use_count` is
the larger of the two (`use_count_skill_tool`, `use_count_ledger`), never their
sum. `window_use_count` counts usage.jsonl rows inside `--window-days`
(default 30). If the Skill-tool record has its last use inside the window, the
count is at least 1. It is a lower bound. A usage.jsonl row older than the
skill's curator record belongs to an earlier skill with the same name and is
not counted, matching the curator.

Missing telemetry is never treated as zero use (#1739, #1675). Each node
reports `usage_ledgers.<provider>.evidence`:

- `ok`: a ledger holds real use.
- `missing`: neither ledger has ever recorded a use.
- `unreadable`: a ledger exists but is unsafe or unreadable.

If any node is not `ok`, the skills it holds are left out of the archive and
unused lists, and the node appears under `evidence_gaps`.

## Origins

The origin comes from the ownership contract
(`claude/hooks/skill-review/ownership.py`):

| Origin | Ownership class | Provenance |
|---|---|---|
| `repo-managed` | `managed/bundled` | `.ccc-node-managed.json` (setup/self-update) |
| `fleet-managed` | `fleet-managed` | `.ccc-fleet-skill.json` (`ccc-fleet-skills-sync.py`) |
| `autosave` | `autosave-managed` | `.autosave-meta.json` (learning pipeline) |
| `external` | `external/repo-installed` | `.git`, `.repo-installed.json`, `.external-skill.json`, symlinked skill |
| `user` | `user-owned` | no marker |
| `unknown` | `unknown/unreadable` | unsafe or invalid metadata |

`fleet-managed` became its own class in #2239. Before that, a fleet-skills
install had no ownership marker of its own and was classified `user-owned`.
The class is:

- never autonomous-writable, never adoptable (`adopt_denied_fleet-managed`)
  and never `mark-created`;
- never a local curator lifecycle target. `run` reports it as `protect` with
  reason `fleet-managed`, and a manual `curator.py archive` is denied with
  `lifecycle_denied_fleet-managed`. A fleet skill is retired through the
  fleet-skills repo (`approved/` → `retired/` via `ccc-skill-retire.py`, or `graduated/`), and the sync
  then reconciles every node;
- still pinnable and unpinnable, so a pin set while it was classified
  `user-owned` can still be removed;
- higher in precedence than an autosave marker in the same directory (reason
  `fleet-marker-conflicts-with-autosave`), following the layering in
  [`skill-graduation.md`](skill-graduation.md#precedence-contract);
- fail-closed. A marker that is not `0600`, names another manager or another
  provider, or does not parse is classified `unknown/unreadable`.

## `node`: one node

```bash
python3 scripts/ccc-skill-usage-report.py node            # text table
python3 scripts/ccc-skill-usage-report.py node --json     # for aggregate
python3 scripts/ccc-skill-usage-report.py node --json --provider claude --provider codex
```

Roots resolve the same way as in the curator and ownership tools: `CCC_CLAUDE_DIR`
(default `~/.claude`), `CLAUDE_SKILLS_DIR`, and `CCC_SKILL_REVIEW_STATE_DIR`
for state. `--provider` (repeatable, default `claude`) adds the codex, piri or
danso roots. The node name is `CCC_NODE`, falling back to the hostname.

Each skill entry contains: `name`, `provider`, `origin`, `classification`,
`pinned`, `use_count` (`_skill_tool`, `_ledger`), `window_use_count`,
`runtimes`, `last_used_at`, `created_at`, `installed_at`, `curator_state`,
`stale_marked_at` and `usage_evidence`. `installed_at` is the mtime of the
fleet marker, which records when the current version was installed. The sync
rewrites the marker on every update, so it is an upper bound for the first
install.

## `aggregate`: the fleet

Collecting is left to the caller. Gather one JSON line per node, then
aggregate:

```bash
# NODES: your ssh aliases, e.g. read from the node-local topology config
for n in $NODES; do
  ssh "$n" 'python3 ~/ccc-node/scripts/ccc-skill-usage-report.py node --json'
done > /tmp/skill-usage.jsonl
python3 scripts/ccc-skill-usage-report.py aggregate /tmp/skill-usage.jsonl
python3 scripts/ccc-skill-usage-report.py aggregate --json < /tmp/skill-usage.jsonl
```

Each node's checkout path may differ (`/opt/ccc-node`, `~/ccc-node`); use the
checkout that node's harness was installed from. When several reports exist
for one node, the newest `generated_at` wins. Idle days are measured against
the newest report time, so re-running over the same files gives the same
result.

| Output | Rule | Flag (default) |
|---|---|---|
| `archive_candidates` | autosave-origin skill whose last use (or creation, or fleet install time) is at least the threshold old on every node that holds the name, under any origin. Shows `curator_state` and `stale_marked_at` per node. | `--stale-days` (30, the curator's `STALE_AFTER_DAYS`) |
| `graduation_candidates` | fleet-managed skill used on at least N nodes inside the window, or with at least M windowed uses summed across nodes. `dwell` is `met` when the earliest install is at least the dwell age old. `unconfirmed` is not a failure; check the intake PR merge date. | `--graduation-min-nodes` (2), `--graduation-min-uses` (3), `--dwell-days` (14) |
| `retire_candidates` | fleet-managed on **every** holder, never used on **any** node, held by at least N nodes, every holder with `ok` evidence, not a graduation candidate (#2239 L1, owner decision 2026-10-22, Wiki DOC-3903 P9). Usage facts only: `{name, nodes, holder_count}`. The age rule is applied by `ccc-skill-retire.py` (below), not here. | `--retire-min-nodes` (3) |
| `unused` | installed on at least one node and never used on any node. Every holder must have `ok` evidence. | none |
| `totals.by_origin` | distinct `skills`, `installs` (node × skill) and `used_installs` per origin | none |
| `evidence_gaps` | node/provider pairs whose ledgers cannot support idleness | none |
| `lifecycle` | skill lifecycle table (#2283, Wiki DOC-3903 TM-3906): `stages` (draft count per lifecycle stage, summed over nodes), `archive_reasons` (the `archived:*` stages), `pending_dwell_days` (`count`/`median`/`p90`/`max` whole days since `staged_at` for drafts still undecided — `drafted` or `prescreened:*`), `nodes` (per-node drafts, pending, dwell median, fleet `installed.json` rows) and `nodes_without_index` (reports without a readable `pending-skills/index.jsonl`, including reports from before #2283) | none |

The window comes from the node reports (`node --window-days`). The aggregate
echoes the distinct values it saw.

The lifecycle table reads each node's `pending-skills/index.jsonl` (rebuilt by
every autosave sweep, `skill-review.sh reindex`) with the last row per draft
winning. Rows written before #2283 have no `lifecycle` field; their stage is
derived from `status`/`prescreen` with the same rules `reindex` uses, so the
table works on today's indexes. Counters and day counts only — no names,
paths or bodies.

An owner `/skillsuggest` reject is the archive reason `archived:owner-rejected`
(#2373). `verdict:{approve|revise|reject}` is reserved for the P6 fleet intake
review recorded in the publisher ledger, which never writes a node index, so a
node-index or node-report `verdict:reject` (written before #2373, or reported
by a node that has not updated yet) is counted as `archived:owner-rejected`.

An approved or installed draft is a node-local install, written
`local-installed:{auto|owner|manual}` (#2374): `auto` = the autosave
installer, `owner` = an owner `autosave apply` (autosave marker, promotable to
the fleet), `manual` = the hand-copied legacy approve (no marker, never
promoted). `approved` and `installed@<sha>` are reserved for the P6 promote
merge and the P7 fleet install (`installed.json`, shown as `fleet_installed`).
A node-index `installed` / `installed@<sha12>` is read as
`local-installed:auto`; a node-index `approved` is read as
`local-installed:owner`, because a row or node report from before #2374 cannot
tell owner from manual — `reindex` recomputes the split from each draft's meta
on that node's next sweep.

A listed candidate is evidence, not an action. The #1648 retirement
precondition still applies: at least 30 days of post-activation records on
more than 3 measured nodes. Archive moves remain PR-first with owner approval.
Graduation still needs every item on the
[`skill-graduation.md`](skill-graduation.md) checklist.

## Retiring unused fleet skills (`ccc-skill-retire.py`, #2239 L1)

`scripts/ccc-skill-retire.py` turns `retire_candidates` into a fleet-skills
pull request that moves each skill from `approved/<audience>/<name>/` to
`retired/<audience>/<name>/` (`git mv`, history and `approval.json` kept) and
writes `retirement.json` beside it. Policy source: ccc-node#2239 L1 (owner
decision 2026-10-22 "1"), Wiki DOC-3903 P9. The lane itself —
`retired/` layout and its validator — is fleet-skills#565.

```bash
# usage facts from the fleet (see `aggregate` above)
python3 scripts/ccc-skill-usage-report.py aggregate --json /tmp/skill-usage.jsonl > /tmp/aggregate.json
# read-only plan: ages from a fresh read-only clone, or from an existing one
python3 scripts/ccc-skill-retire.py plan --aggregate /tmp/aggregate.json
python3 scripts/ccc-skill-retire.py plan --aggregate /tmp/aggregate.json --fleet-skills-dir ~/work/fleet-skills
# the PR (dry run without --yes)
python3 scripts/ccc-skill-retire.py pr --aggregate /tmp/aggregate.json
python3 scripts/ccc-skill-retire.py pr --aggregate /tmp/aggregate.json --yes
```

Selection, in order, per candidate (names sorted): `--exclude NAME` →
`excluded`; a directory already under `retired/*/NAME` → `already_retired`;
no `approved/<audience>/NAME` (audiences `shared|claude|codex|piri|danso`) →
`not_in_approved`; `approval.json` `approved_at` younger than
`--min-idle-days` (default 60) → `approved_too_recent`; beyond `--max`
(default 50) → `over_max`. Everything else is selected with its `audience`,
`approved_at` and `idle_days`.

**Why `approved_at` is the age anchor.** A node's `installed_at` is the
`.ccc-fleet-skill.json` mtime, and the sync rewrites that marker on every ref
change — after the 2026-10-21 sync all 12 nodes reported the same day for
every fleet skill, so "days since this skill reached the fleet" cannot be
read from node data. `approved_at` in the canon repository is when
distribution started and is immutable on move.

`retirement.json` (validated by fleet-skills; errors `retirement_missing`,
`retirement_schema_invalid`, `retirement_reason_invalid`,
`retirement_source_commit_invalid`, `retirement_evidence_missing`,
`retirement_evidence_invalid`, `retired_name_still_approved`):

```json
{
  "schema_version": 1,
  "retired_at": "2026-10-22T09:00:00Z",
  "reason": "unused",
  "source_commit": "<fleet-skills HEAD at clone, 40 hex>",
  "evidence": {
    "report_generated_at": "<aggregate generated_at>",
    "nodes_measured": 12,
    "min_idle_days": 72,
    "aggregate_sha256": "<sha256 of the aggregate file>"
  }
}
```

The PR is non-draft and titled `retire: N unused fleet skills (≥60d idle on
all nodes, 0 Skill calls)`; its body is a table `name | holders | approved_at
| idle_days | audience` plus thresholds and the aggregate sha. **It needs
human approval (#1739)** — the tool never approves, merges or enqueues.
Merging makes every node prune the skill on its next sync
(`ccc-fleet-skills-sync.py` `RETIRE_PRUNE`, backup kept under the node's
fleet-skills backups); restoring is a reverse-move PR. Output is one JSON
object `{ok, mode, selected, skipped, pr_url?, branch?, head?}` with names
and counters only; `pr --yes` refuses `--fleet-skills-dir` (it always works
in a fresh 0700 clone) and reports `nothing_to_retire` when nothing qualifies.

## Two-stage curation with archive enabled

The curator's default lifecycle is unchanged: mark only, with automatic
archive off (`CCC_SKILL_CURATOR_ARCHIVE_ENABLED=false`, #2011).

1. **Stage 1:** a skill idle past `CCC_SKILL_CURATOR_STALE_AFTER_DAYS` (30) is
   marked `stale` and `stale_marked_at` is stamped. The skill stays live.
2. **Stage 2:** a skill becomes an `archive-candidate` when it is still idle a
   full `CCC_SKILL_CURATOR_RECHECK_AFTER_DAYS` (30) after the mark and has not
   been used since.

From #2239, setting `CCC_SKILL_CURATOR_ARCHIVE_ENABLED=true` does not skip
either stage. The curator moves a skill only if all of these hold:

- it was already marked `stale` (`stale_marked_at` is set);
- at least `RECHECK_AFTER_DAYS` have passed since the mark;
- it has had no use since the mark;
- its idle time is at least `CCC_SKILL_CURATOR_ARCHIVE_AFTER_DAYS` (default
  90).

`ARCHIVE_AFTER_DAYS` is kept as a minimum idle time, so turning archive on can
only slow the lifecycle down compared with mark-only. Set it to `60` for the
effective 30 + 30 days in #1739 v2. A stage-2 skill below the minimum is
reported as `archive-candidate` with reason
`…;below-archive-floor-<N>d`.

### Where a node sets these

The managed autosave cron line has no curator options, so the nightly sweep
(`scripts/ccc-skill-autosave.sh`) reads an owner-only settings file:

```bash
# ~/.config/ccc-node/skill-curator.env  (mode 0600, owned by the sweep's user)
CCC_SKILL_CURATOR_ARCHIVE_ENABLED=true
CCC_SKILL_CURATOR_ARCHIVE_AFTER_DAYS=60
```

- Override the path with `CCC_SKILL_CURATOR_ENV_FILE`.
- The file is **parsed, never sourced**. Only `ENABLED`, `ARCHIVE_ENABLED`,
  `ARCHIVE_AFTER_DAYS`, `STALE_AFTER_DAYS`, `RECHECK_AFTER_DAYS` and
  `BACKUP_KEEP` (each prefixed `CCC_SKILL_CURATOR_`) with a short
  `[A-Za-z0-9._-]` value are taken; any other line is ignored.
- A value already in the environment (cron line, operator export) wins.
- A symlink, a file owned by someone else, or one with group/other bits is
  ignored and shown as `unsafe` in `ccc-skill-autosave.sh status`.
- Each sweep logs `curator config file=<state> applied=<key names>` and the
  effective archive state and floor. Values are not logged.

Turning archive on is an owner decision (#1739: PR-first, owner approval,
move only, never delete). Running `curator.py` by hand does not read this
file; export the same variables for a manual `run --dry-run`.

Usage clears or restarts the mark:

- A use seen by the next run reactivates the skill: the state returns to
  `active` and `stale_marked_at` is cleared.
- If the skill was used after the mark but is idle again by the next run, the
  recheck window restarts (`used-since-mark-recheck-restart`).
- A restored skill starts again at stage 1 and is never archived again on the
  next run.

Restore and backup behave as before.
