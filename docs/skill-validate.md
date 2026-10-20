# Skill pre-validation (#2386)

A skill can be broken in ways nothing reports at load time: a SKILL.md
without `description:` is dropped silently, a core skill whose description
does not say when to use it (or runs long) wastes the per-turn listing budget,
a SKILL.md that runs `python3 scripts/x.py` fails only when the agent reaches
that step, and a malformed hook never fires
instead of erroring. `scripts/ccc-skill-validate.py` catches those shapes
before they ship. It is read-only, stdlib only, and deterministic (sorted
findings, stable JSON).

## Rules

| Rule id | Default | Fires when |
|---|---|---|
| `frontmatter-missing` | error | SKILL.md does not open with a `---` block, or the block is never closed |
| `description-missing` | error | no `description:`, or it is empty |
| `name-missing` | warn | no `name:` (Claude Code falls back to the directory name) |
| `name-mismatch` | error | `name:` differs from the skill directory name |
| `description-banned-word` | warn | the description uses `benchmark` / `evaluation` / `grader` / `rubric` / `score` (plural too, whole words, any case): measurement wording invites grader-gaming text |
| `core-description` | error | a skill named in `claude/skill-listing-core.txt` (and present under a scanned root) whose `description:` does not start with `Use when` / `Use whenever` (any case, after trimming) or is longer than 300 characters (block scalars folded first). Skills not in the core list are not subject; core names absent from the roots are ignored |
| `script-ref-missing` | error | SKILL.md runs `scripts/<x>.py` or `.sh` anchored to its own directory (`$SKILL_DIR/`, `${CLAUDE_SKILL_DIR}/`, `<skill-root>/`, `.../skills/<name>/`) and the file is not there |
| `script-ref-unresolved` | warn | a bare `scripts/<x>` (or `$ROOT/`, `~/`, absolute) reference found neither in the skill directory nor under a script base |
| `py-compile` | error | a `*.py` inside a skill directory, or a resolved `.py` reference, does not compile |
| `hooks-schema` | error | a settings/hooks JSON `hooks` block is not `{event: [{matcher?, hooks: [{type, command, timeout?}]}]}`: an empty `command` (or `prompt` / `url` for those hook types), a missing `type`, a non-string `matcher`, an empty group, a non-positive `timeout` |
| `hooks-unknown-event` / `hooks-unknown-type` | warn | event name or hook type outside the known set; a typo never fires |

Script references are found per whitespace/quote/backtick-delimited token, so
URLs are skipped. Absolute paths are never looked up on the host. A
`scripts/lib/x.sh:12-14`-style citation counts as a reference to
`scripts/lib/x.sh`.

`--strict` turns every warning into an error (notes never fail; no current
rule emits one).
`--warn-rule RULE` (repeatable) demotes an error rule to a warning. Exit status
`0` = no failing finding, `1` = at least one error, `2` = usage error.

## Modes

```bash
# repo mode (default): skills/, codex/skills/, piri/skills/ plus
# claude/settings.base.json and claude/hooks/{hooks,enforcement-overlay}.json;
# core list = claude/skill-listing-core.txt; script base = the repo root
python3 scripts/ccc-skill-validate.py

# node mode: any --root (repeatable); settings.json and a script base come from
# --claude-dir (default $CCC_CLAUDE_DIR or ~/.claude); the checkout the script
# runs from is a script base too and supplies the core list
python3 scripts/ccc-skill-validate.py --root ~/.claude/skills --json
```

`--settings`, `--script-base` (all repeatable) and `--core-list` replace the
defaults of either mode. Dot-directories under a root (install staging such as
`.stage-<name>`) are skipped.

## Core skills: description, not trigger lines

Owner decision 2026-10-20 (#2386 follow-up): core skills are **not** given
separate trigger lines in the always-on text (CLAUDE.md, `tools-cheatsheet.md`,
output styles). `scripts/ccc-skill-listing-policy.py` keeps every core skill's
description in Claude Code's per-turn skill listing
([`skill-listing-budget.md`](skill-listing-budget.md), #2011), so a
"Use when ..." description already surfaces the skill on every turn; an extra
always-on line would duplicate it and spend always-on budget, which one node
measured at 104% of its 24,000-char cap (#2384). The validator therefore checks
the description itself (`core-description`), and the earlier
`trigger-core-missing` / `trigger-missing` rules, `--trigger-file` and
`--autotrigger --dry-run` were removed.

The rule checks every scanned root, so a Codex skill that shares a core name
(`codex/skills/gh-pr-flow`) follows the same shape. The 350-char hard cap in
`scripts/ccc_skill_listing_policy_test.py` stays as the policy test's ceiling;
this validator enforces the documented 300.

## CI

`scripts/validate-harness.sh` (static phase) runs repo mode with every rule at
its default severity (no `--warn-rule`), plus
`scripts/ccc_skill_validate_test.py`.

## Out of scope

The listing budget ([`skill-listing-budget.md`](skill-listing-budget.md),
#2011) and skill-body quality review. The usage ledger
([`skill-usage-report.md`](skill-usage-report.md), #2239) answers "never
loaded"; picking archive candidates from it (#1739) is a separate step.
