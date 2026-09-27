# Skill-listing budget policy (#2011 A)

Claude Code injects a listing of every available skill into each turn, capped
at `skillListingBudgetFraction` of the context window (default `0.01`). When
the listing is over budget it keeps every name but only describes the most-used
skills; the rest are listed as bare names, so the model cannot tell when to use
them. On one fleet node (2026-09-27, 189 skills, ~40k chars of descriptions) sessions
listed 206–209 skills with only 87–88 described, and because usage data existed
for just 13 skills the described set was effectively alphabetical — the fleet
workflows were often among the name-only ones.

`scripts/ccc-skill-listing-policy.py` (installed as
`~/.claude/hooks/ccc-skill-listing-policy.py`) makes that choice explicit and
deterministic.

## Policy

For every `~/.claude/skills/<dir>/SKILL.md` (skills with
`disable-model-invocation: true` are not listed and are skipped):

| Condition | Result |
|---|---|
| operator wrote a `skillOverrides` entry | left exactly as written |
| name in the core list (`claude/skill-listing-core.txt`) | description kept (no entry) |
| used within the last 30 days (`--days`, `CCC_SKILL_LISTING_RECENT_DAYS`) | description kept (no entry) |
| anything else | `skillOverrides[<name>] = "name-only"` |

"Used" means a record in `state/skill-usage/usage.jsonl` (Skill tool or a Read
of the skill's `SKILL.md`, `skill-usage-log.sh`) or a `claude:`-lane
`last_used_at` / `last_viewed_at` in `state/skill-autosave-usage.json`.
`skillListingBudgetFraction` is set to `0.02` only when the key is absent.

A `name-only` skill is still listed and still invocable by name
(`/<name>` or the Skill tool); it just costs no description budget. The policy
**never writes `"off"`** and never deletes or moves a skill — retirement stays
with the #1739 owner decision (archive only, PR-first, owner-approved).

## Ownership

The policy owns only the entries it created, recorded in
`state/skill-listing-policy.json`. An entry is still the policy's only while
its value is exactly the `"name-only"` it wrote; if an operator edits it (for
example to `"on"`) it becomes operator-owned and is never touched again. Any
key the policy did not create — including `"off"` or `"user-invocable-only"`
— is operator-owned. To pin a skill's description on a node, write
`"skillOverrides": {"<name>": "on"}`.

`settings.json` is re-rendered by `setup.sh` on every self-update; setup
captures `skillOverrides`, `skillListingBudgetFraction`, and
`skillListingMaxDescChars` before the render and restores them afterwards
(template-declared keys would win, as for `model`/`env`), then runs the policy.

## When it runs

- `setup.sh`, after the settings merge and the repo-skill install
  (`plan --summary` under `--dry-run`). A policy failure only logs a warning; it
  never rolls back the install.
- Daily, right after the fleet-skills sync in the existing
  `# ccc-node:fleet-skills-sync` cron entry
  (`scripts/install-fleet-skills-sync-cron.sh`). It runs even when the sync
  fails, and the entry keeps the sync's exit status. Self-update re-renders the
  entry automatically when the installer's gen stamp changes.

## Commands

```bash
python3 ~/.claude/hooks/ccc-skill-listing-policy.py plan            # read-only, per-skill decisions + estimate
python3 ~/.claude/hooks/ccc-skill-listing-policy.py plan --json
python3 ~/.claude/hooks/ccc-skill-listing-policy.py apply           # idempotent
python3 ~/.claude/hooks/ccc-skill-listing-policy.py release         # remove every policy-owned entry
```

`plan` prints an **estimate** of the listing size before/after
(`- name: description` per skill, descriptions capped at
`skillListingMaxDescChars`, default 1536) against a budget of
`fraction × --context-tokens (200000) × 4` chars. Claude Code's exact
accounting differs; use `/context` in a session for the real number. If the
estimate stays over budget, trim the core list rather than raising the
fraction.

`apply` writes only when the rendered result differs: it backs up
`settings.json` to `~/.claude/backups/skill-listing-policy/` (newest 10 kept),
replaces it atomically with the file mode preserved, and refuses to overwrite a
file that changed while it was planning. Invalid JSON, a non-object
`skillOverrides`, a symlinked `settings.json`, or a missing core list fail
closed (exit 2, nothing written).

Kill switch: `CCC_SKILL_LISTING_POLICY=0` or the file
`~/.claude/skill-listing-policy.disabled` makes `apply` a no-op; follow with
`release` to undo what it wrote.

## Core list

`claude/skill-listing-core.txt`, one name per line, installed next to the
script. It holds the fleet workflows every node should always see described:
the repo-shipped operational skills plus a few fleet-installed flows
(`a2a-task-poll`, `remote-node-harness-sync`, `model-migrate`). Keep it short —
every entry costs budget on every turn of every session. Names not installed on
a node are ignored; custom commands under `~/.claude/commands` are not managed
by the policy and need no entry.
