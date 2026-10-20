# Skill pre-validation (#2386)

A skill can be broken in ways nothing reports at load time: a SKILL.md
without `description:` is dropped silently, a skill nobody's always-on text
names is rarely triggered, a SKILL.md that runs `python3 scripts/x.py` fails
only when the agent reaches that step, and a malformed hook never fires
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
| `trigger-core-missing` | error | a skill named in `claude/skill-listing-core.txt` whose directory name never appears as a whole token in the always-on text (CLAUDE.md, `tools-cheatsheet.md`, output styles) |
| `trigger-missing` | note | the same for a non-core skill (reported, never fails) |
| `script-ref-missing` | error | SKILL.md runs `scripts/<x>.py` or `.sh` anchored to its own directory (`$SKILL_DIR/`, `${CLAUDE_SKILL_DIR}/`, `<skill-root>/`, `.../skills/<name>/`) and the file is not there |
| `script-ref-unresolved` | warn | a bare `scripts/<x>` (or `$ROOT/`, `~/`, absolute) reference found neither in the skill directory nor under a script base |
| `py-compile` | error | a `*.py` inside a skill directory, or a resolved `.py` reference, does not compile |
| `hooks-schema` | error | a settings/hooks JSON `hooks` block is not `{event: [{matcher?, hooks: [{type, command, timeout?}]}]}`: an empty `command` (or `prompt` / `url` for those hook types), a missing `type`, a non-string `matcher`, an empty group, a non-positive `timeout` |
| `hooks-unknown-event` / `hooks-unknown-type` | warn | event name or hook type outside the known set; a typo never fires |

Script references are found per whitespace/quote/backtick-delimited token, so
URLs are skipped. Absolute paths are never looked up on the host. A
`scripts/lib/x.sh:12-14`-style citation counts as a reference to
`scripts/lib/x.sh`.

`--strict` turns every warning into an error (notes never fail).
`--warn-rule RULE` (repeatable) demotes an error rule to a warning. Exit status
`0` = no failing finding, `1` = at least one error, `2` = usage error.

## Modes

```bash
# repo mode (default): skills/, codex/skills/, piri/skills/ plus
# claude/settings.base.json, claude/hooks/{hooks,enforcement-overlay}.json and
# the always-on text claude/CLAUDE.md.template, claude/hooks/tools-cheatsheet.md,
# claude/output-styles/*.md; script base = the repo root
python3 scripts/ccc-skill-validate.py

# node mode: any --root (repeatable); always-on text, settings.json and a
# script base come from --claude-dir (default $CCC_CLAUDE_DIR or ~/.claude);
# the checkout the script runs from is a script base too
python3 scripts/ccc-skill-validate.py --root ~/.claude/skills --json

# core skills without a trigger line: print one suggested line each
python3 scripts/ccc-skill-validate.py --root ~/.claude/skills --autotrigger --dry-run
```

`--settings`, `--trigger-file`, `--script-base` (all repeatable) and
`--core-list` replace the defaults of either mode. Dot-directories under a root
(install staging such as `.stage-<name>`) are skipped.

`--autotrigger` only runs with `--dry-run`: it prints
`- When <description, first clause>, load the <name> skill.` for every core
skill without a trigger and never writes. Adding the line to CLAUDE.md or the
cheatsheet is a reviewed PR, because every always-on line costs context on
every turn.

## CI

`scripts/validate-harness.sh` (static phase) runs repo mode with
`--warn-rule trigger-core-missing`, plus `scripts/ccc_skill_validate_test.py`.
Every other rule is enforced at its default severity on the shipped tree.

The trigger rule is report-only in CI because on the 2026-10-20 tree 12 of
the 16 repo-shipped core skills are not named in the repo's always-on text
(only `distill`, `mcp-add`, `skillsuggest` and `wiki-record` are). Making it an
error needs a PR that adds trigger lines (use `--autotrigger --dry-run` for
drafts) and then drops the `--warn-rule` from validate-harness.sh.

## Out of scope

The listing budget ([`skill-listing-budget.md`](skill-listing-budget.md),
#2011) and skill-body quality review. The usage ledger
([`skill-usage-report.md`](skill-usage-report.md), #2239) answers "never
loaded"; crossing it with `trigger-missing` for archive candidates (#1739) is a
separate step.
