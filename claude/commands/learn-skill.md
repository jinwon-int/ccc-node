---
description: Learn a reusable Claude Code skill from the given workflow/source and stage it for human approval.
argument-hint: [workflow, URL, directory, or "what we just did"]
allowed-tools: Bash(mkdir:*), Bash(date:*), Bash(jq:*), Bash(sed:*), Bash(awk:*), Bash(cp:*), Bash(mv:*)
---

## Task

Create **one** reusable `SKILL.md` draft from this source and stage it under the human-gated queue. Do **not** install it directly into `~/.claude/skills` unless the operator explicitly approves after review.

**Source to learn from:** `$ARGUMENTS`

## Required output path

Use this queue shape:

```text
${CCC_STATE_DIR:-$HOME/.claude/state}/pending-skills/<timestamp>-<skill-name>/
  SKILL.md
  meta.json
```

## Authoring rules

- Name: lowercase kebab-case, class-level, not a PR/issue/session artifact.
- Frontmatter: `name:` and `description:`.
- Body sections: `When to Use`, `Procedure`, `Safety`, `Verification`.
- Public-safe only. No raw secrets, tokens, private endpoints, message text, credential values, or node-specific mutable facts.
- Prefer exact commands only when they are visible in the provided source; otherwise encode the decision rule and verification contract.
- If the source is ambiguous, make a reasonable bounded assumption and record it in `meta.json`.

## Staging commands

After drafting the full `SKILL.md`, stage it like this, replacing placeholders:

```bash
STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
name="<skill-name>"
id="$(date -u +%Y%m%d-%H%M%S)-$name"
mkdir -p "$STATE/pending-skills/$id"
# write the draft to "$STATE/pending-skills/$id/SKILL.md"
jq -nc --arg id "$id" --arg name "$name" --arg source "$ARGUMENTS" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{id:$id,name:$name,status:"pending",origin:"learn-skill",source:$source,staged_at:$at}' \
  > "$STATE/pending-skills/$id/meta.json"
printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "PENDING_SKILL_REVIEW staged=1 origin=learn-skill name=$name" \
  >> "$STATE/approval-needed.log"
```

Then summarize the staged id and tell the operator to review with `/skill-suggest` before approval.
