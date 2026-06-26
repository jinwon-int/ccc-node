---
name: skill-suggest
description: Detect frequently-repeated procedures from this node's Claude Code transcripts and propose new skills (human-in-the-loop). Use when asked to find automatable routines, "what should be a skill", or to review skill candidates. Scans transcripts, ranks repeated command shapes, and drafts SKILL.md proposals for approval — it never installs a skill without the user's OK.
---

# skill-suggest — propose skills from repeated work (human-approved)

Approximates "auto-skillification": find procedures you keep repeating and turn the good ones into skills. Detection is automatic; **authoring/installation requires user approval** (no silent skill creation). Hermes-style Skill Review may also stage draft `SKILL.md` packages under `~/.claude/state/pending-skills/` after SessionEnd.

## Procedure

1. **Check pending Skill Review drafts first** (LLM-drafted, still human-gated):
   ```bash
   STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
   find "$STATE/pending-skills" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort
   ```
   For a chosen `<id>`:
   ```bash
   STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
   jq . "$STATE/pending-skills/<id>/meta.json"
   sed -n '1,220p' "$STATE/pending-skills/<id>/SKILL.md"
   ```
   Approve only after reading the full draft and checking for node-specific facts or secrets:
   ```bash
   STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
   id="<id>"
   name="$(jq -r '.name // empty' "$STATE/pending-skills/$id/meta.json")"
   [ -n "$name" ] || name="$(awk 'NR>1 && /^---/{exit} /^name:/ {sub(/^name:[[:space:]]*/,""); print; exit}' "$STATE/pending-skills/$id/SKILL.md")"
   mkdir -p "$HOME/.claude/skills/$name"
   cp "$STATE/pending-skills/$id/SKILL.md" "$HOME/.claude/skills/$name/SKILL.md"
   jq '.status="approved"' "$STATE/pending-skills/$id/meta.json" > "$STATE/pending-skills/$id/meta.approved.json"
   mv "$STATE/pending-skills/$id" "$STATE/pending-skills/$id.approved-$(date -u +%Y%m%d%H%M%S)"
   ```
   Reject by archiving rather than deleting:
   ```bash
   STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
   mv "$STATE/pending-skills/<id>" "$STATE/pending-skills/<id>.rejected-$(date -u +%Y%m%d%H%M%S)"
   ```

2. **Refresh deterministic candidates** (command-shape scan of transcripts):
   ```bash
   bash ~/.claude/skills/skill-suggest/scan.sh
   cat ~/.claude/state/skill-candidates.md
   ```
   (A daily cron may also refresh this file, so candidates stay current between sessions.)

3. **Interpret, don't dump.** Read the ranked command shapes and cluster them into *procedures*:
   - Group related high-count shapes by tool/intent (e.g. `wiki-agent write-path` + `wiki-agent pr` = the wiki flow).
   - Drop shapes already covered by an existing skill (listed in the report).
   - Drop one-off/trivial shapes; a skill is worth it only for a multi-step procedure you repeat across sessions.

4. **Propose (max ~3).** For each strong candidate, show the user a short proposal: name, one-line description, the steps it would encode, and why it recurs. Ask for approval (numbered options).

5. **Author on approval only.** For each approved deterministic candidate, create `~/.claude/skills/<name>/SKILL.md`:
   - Frontmatter: `name` (kebab-case) + a detailed `description` (it drives auto-matching).
   - Body: numbered steps, exact commands, and safety rules (no raw secrets — read keys from `~/.hermes/.env`; redact in output).
   - Offer to also land it in the `jinwon-int/ccc-node` template (`claude/skills/`) via PR, and record it in the Wiki (use the `wiki-record` skill).

## Rules
- Never author or overwrite a skill without explicit approval.
- Keep skills node-agnostic where possible; keep secrets out (locations/handling only).
- The scan is a heuristic over command *shapes* — always sanity-check that a candidate is a real recurring procedure before proposing.
