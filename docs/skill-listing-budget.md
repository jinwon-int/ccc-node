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
| used within the last 30 days (`--days`, `CCC_SKILL_LISTING_RECENT_DAYS`) and it fits the budget | description kept (no entry) |
| used within the last 30 days but does not fit (`recent, over budget`) | `skillOverrides[<name>] = "name-only"` |
| anything else, fill mode on and it fits `F × budget` ([fill mode](#fill-mode-2748-default-off)) | description kept (no entry) |
| anything else | `skillOverrides[<name>] = "name-only"` |

"Used" means a record in `state/skill-usage/usage.jsonl` (Skill tool or a Read
of the skill's `SKILL.md`, `skill-usage-log.sh`) or a `claude:`-lane
`last_used_at` / `last_viewed_at` in `state/skill-autosave-usage.json`.
`skillListingBudgetFraction` is set to `0.02` only when the key is absent.

### Budget fit for recent skills (#2031)

Describing every recent skill regardless of size left five nodes over the
estimate after the policy (2026-09-28: 17,511–21,583 chars against 16,000 on
nodes with 133–284 skills). The policy now fits recent skills into the budget:

1. Fixed costs first: operator entries as written, core skills described,
   everything else name-only. Every recent skill starts as name-only.
2. Recent (non-core, non-operator) skills are ranked by most recent use, then
   in-window use count (descending), then name.
3. In that order, a skill keeps its description while the running estimate
   stays at or below the budget. From the first skill that does not fit, it
   and every lower-ranked recent skill become policy-owned `name-only` with
   reason `recent, over budget (used <ts>)`. The policy does not skip over a
   skill to fit a smaller one, so a less recently used skill is never
   described while a more recent one is not.

The budget is the one the settings will have after the run (the key may be set
by that same run) for the node's context window, resolved in this order:
`--context-tokens`, then env `CCC_SKILL_LISTING_CONTEXT_TOKENS`, then the
window implied by `settings.json` `model` (#2108) — 1M for Fable / Mythos,
Opus and Sonnet 4.6+, any `[1m]` id and the `opus` / `sonnet` / `fable` /
`opusplan` aliases; 200000 for Haiku. When `settings.json` has no `model` (or
an unrecognised one), the bridge's `ANTHROPIC_MODEL` is used instead (#2358):
first from the policy's own environment, then from the bridge systemd unit
(`ccc-matrix-bridge.service`, then `ccc-telegram-bridge.service`, read-only
`systemctl show`; `Environment=` then each `EnvironmentFiles=` entry in order,
so the value the bridge process actually receives wins; only that one key is
read). This covers nodes that pin the bridge model in a drop-in env file.
`CCC_SKILL_LISTING_SYSTEMCTL` names the systemctl binary, `off` disables the
lookup. Otherwise the window is 200000 (a too-small budget only name-only's
more, it never overflows).
`plan` / `apply` print the window and its source (`plan --json`:
`context_tokens`, `context_source`). Before #2108 the default was always
200000, so 1M-context nodes were held to a fifth of their real budget and had
recent skills name-only'd that would have fit. The decision depends only on the inputs, so a second
`apply` is a no-op. A skill that fits again later (a skill was archived, the
budget grew, a more recent skill aged out) gets its policy-owned entry removed.
`release` removes every policy-owned entry, including these.

### Fill mode (#2748, default off)

The rules above describe only core and recently used skills, however much of
the budget is left. On a node with a 1M window that is most of it (measured
2026-11-10: 310 skills, 21 described, 19,321 of 80,000 chars — 24%). A
name-only skill is rarely picked, so it never becomes "recently used" and
stays name-only; a freshly installed skill never gets its first use.

Fill mode spends part of the free budget on the "anything else" group. With a
fill fraction `F` (`0 < F ≤ 1`) the policy, after the recent-skill fit,
describes more skills while the estimated listing stays at or below
`F × budget` (`int(F × budget chars)`). Candidates are taken in this order:

| Tier | Which skills | Order inside the tier |
|---|---|---|
| 1 `autosave-unused` | node-local autosave-managed skills (`.autosave-meta.json` naming the autosave manager and the skill's directory) with no recorded Skill-tool use | name |
| 2 `fleet` | fleet-managed skills (`.ccc-fleet-skill.json` written by the fleet-skills sync) | most recently installed/updated first, then name |
| 3 `other` | everything else | name |

- "No recorded use" is the evidence the promotion usage gate counts: a
  `use_count` above 0 in the `claude:` lane of `state/skill-autosave-usage.json`
  or a `Skill` row in `state/skill-usage/usage.jsonl`, at any time. A Read of
  the `SKILL.md` is a view, not a use. These drafts need a first use before
  they can be promoted, which is why they go first.
- "Installed/updated" is a proxy: the mtime of the skill's fleet marker. The
  fleet-skills sync writes the marker when it installs or updates a skill and
  keeps the mtime on a commit-only refresh, so it is the install time of the
  current version on this node (the same reading `ccc-skill-usage-report.py`
  uses). It is not the fleet approval date; after a full reinstall every
  marker is equally new and the tier falls back to name order.
- A skill that carries both markers is `fleet` (a fleet-adopted draft is no
  longer waiting for the promotion gate). A missing, malformed or foreign
  marker only moves a skill to tier 3.
- Filling stops at the first candidate that would cross the limit; it does not
  skip ahead to a smaller one, so a lower-ranked skill is never described
  while a higher-ranked one is not.
- Fill never competes with recently used skills: they are fitted against the
  full budget first, and when any of them is `recent, over budget`, fill
  describes nothing.
- Operator entries are never candidates and are never modified.

Fill mode writes nothing new into `settings.json`. A filled skill simply gets
no `skillOverrides` entry, and a policy-owned `"name-only"` entry for it is
removed; the policy still never writes `"on"`, `"off"` or any other value. No
fill state is stored — the decision depends only on the inputs, so a second
`apply` is a no-op, and `release` works exactly as before (it does not read
the fill switch).

**Turning it on.** The first source that is present decides:

1. `--fill-budget-fraction F` (`plan` / `apply`);
2. env `CCC_SKILL_LISTING_FILL_FRACTION=F`;
3. the node-local file `~/.claude/skill-listing-policy.fill`, whose first
   non-empty, non-comment (`#`) line is `F`. This is the persistent per-node
   switch: `setup.sh` and the daily cron entry run `apply --quiet` unchanged
   and pick it up.

```bash
python3 ~/.claude/hooks/ccc-skill-listing-policy.py plan --summary --fill-budget-fraction 0.5   # preview, read-only
echo 0.5 > ~/.claude/skill-listing-policy.fill                                                  # enable on this node
python3 ~/.claude/hooks/ccc-skill-listing-policy.py apply
rm ~/.claude/skill-listing-policy.fill && python3 ~/.claude/hooks/ccc-skill-listing-policy.py apply   # back to the default
```

`0` / `off` / `false` / `no` in the env or the file means off (the env value
turns a file-enabled fill off for one run). Any other unusable value there —
not a number, outside `(0, 1]`, an empty or oversized file — is ignored: fill
is off, the result is exactly the default one, and a warning goes to stderr
(`fill mode off — <source>: invalid value ignored …`); it does not fall
through to the next source. An invalid `--fill-budget-fraction` is a usage
error (exit 2, nothing written). With no switch present the decisions, the
written `settings.json` and the text output are the same as before fill mode
existed. Turning fill off brings back the same entries; the ones that are
re-added are appended, so only the key order inside `skillOverrides` may
differ from before.

When fill is on, `plan` / `apply` add `fill=<n>` to the `keep=` breakdown and
print one more line (illustrative numbers):

```
  fill: fraction=0.5 (file ~/.claude/skill-listing-policy.fill) limit=40000 chars; described by fill=70 (autosave-unused=4/4 fleet=66/250 other=0/15); estimated listing=39817/80000 chars (49.8% of budget)
```

Each tier shows `described by fill / candidates`. Per-skill lines read
`keep <name> [fill: <tier>]`. `plan --json` carries the same under `fill`
(`enabled`, `fraction`, `source`, `note`, and when enabled `limit_chars`,
`promoted`, `candidates`, `promoted_total`).

**Cost.** Every description is injected into every session. At an average of
~325 chars a description, 50% of an 80,000-char budget is about 70 more
described skills and roughly 5K more tokens per session; 80% is about 11K
more. Start low, and compare first uses of non-core skills with the mode on
and off before raising it.

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
captures every top-level key the repo templates do not declare — including
`skillOverrides`, `skillListingBudgetFraction`, and `skillListingMaxDescChars`
(#1920) — before the render and restores them afterwards (template-declared
keys would win, as for `model`/`env`), then runs the policy.

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
python3 ~/.claude/hooks/ccc-skill-listing-policy.py plan --summary --fill-budget-fraction 0.5   # preview fill mode (#2748)
python3 ~/.claude/hooks/ccc-skill-listing-policy.py release         # remove every policy-owned entry
```

`plan` prints an **estimate** of the listing size before/after
(`- name: description` per skill, descriptions capped at
`skillListingMaxDescChars`, default 1536) against a budget of
`fraction × context window × 4` chars (window resolved as above). Claude Code's exact
accounting differs; use `/context` in a session for the real number.

The estimate breaks down into described chars (of which core), name-only
chars, and `over_budget` (`plan --json`: `after.over_budget`,
`after.described_chars`, `after.core_desc_chars`, `after.name_only_chars`,
and a top-level `warning`). When the estimate is still over budget after the
fit, every recent skill is already name-only, and a `WARNING:` line names the
dominant cost and what to trim:

- **core descriptions** — shorten the core skills' `description:` (keep it
  under ~300 chars, trigger first) or trim the core list;
- **name list** — too many installed skills; reduce the count (archive per
  #1739, owner-approved);
- **other kept descriptions** — operator `"on"` pins; review them.

Do not raise the fraction to hide it.

`apply` writes only when the rendered result differs: it backs up
`settings.json` to `~/.claude/backups/skill-listing-policy/` (newest 10 kept),
replaces it atomically with the file mode preserved, and refuses to overwrite a
file that changed while it was planning. Invalid JSON, a non-object
`skillOverrides`, a symlinked `settings.json`, or a missing core list fail
closed (exit 2, nothing written).

Kill switch: `CCC_SKILL_LISTING_POLICY=0` or the file
`~/.claude/skill-listing-policy.disabled` makes `apply` a no-op; follow with
`release` to undo what it wrote. The kill switch wins over the fill switch.

## Core list

`claude/skill-listing-core.txt`, one name per line, installed next to the
script. It holds the fleet workflows every node should always see described:
the repo-shipped operational skills plus a few fleet-installed flows
(`a2a-task-poll`, `remote-node-harness-sync`, `model-migrate`). Keep it short —
every entry costs budget on every turn of every session, and core costs are
paid before any recent skill is described. Keep core descriptions short: a
"Use when …" trigger first, ≤300 chars (350 hard cap, enforced for the
repo-shipped ones by `scripts/ccc_skill_listing_policy_test.py`); put the
detail in the SKILL.md body. Names not installed on
a node are ignored; custom commands under `~/.claude/commands` are not managed
by the policy and need no entry.
