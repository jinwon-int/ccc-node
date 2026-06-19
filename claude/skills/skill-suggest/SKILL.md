---
name: skill-suggest
description: Detect frequently-repeated procedures from this node's Claude Code transcripts and propose new skills (human-in-the-loop). Use when asked to find automatable routines, "what should be a skill", or to review skill candidates. Scans transcripts, ranks repeated command shapes, and drafts SKILL.md proposals for approval — it never installs a skill without the user's OK.
---

# skill-suggest — propose skills from repeated work (human-approved)

Approximates "auto-skillification": find procedures you keep repeating and turn the good ones into skills. Detection is automatic; **authoring requires user approval** (no silent skill creation).

## Procedure

1. **Refresh candidates** (deterministic scan of transcripts):
   ```bash
   bash ~/.claude/skills/skill-suggest/scan.sh
   cat ~/.claude/state/skill-candidates.md
   ```
   (A daily cron also refreshes this file, so candidates stay current between sessions.)

2. **Interpret, don't dump.** Read the ranked command shapes and cluster them into *procedures*:
   - Group related high-count shapes by tool/intent (e.g. `wiki-agent write-path` + `wiki-agent pr` = the wiki flow).
   - Drop shapes already covered by an existing skill (listed in the report).
   - Drop one-off/trivial shapes; a skill is worth it only for a multi-step procedure you repeat across sessions.

3. **Propose (max ~3).** For each strong candidate, show the user a short proposal: name, one-line description, the steps it would encode, and why it recurs. Ask for approval (numbered options).

4. **Author on approval only.** For each approved candidate, create `~/.claude/skills/<name>/SKILL.md`:
   - Frontmatter: `name` (kebab-case) + a detailed `description` (it drives auto-matching).
   - Body: numbered steps, exact commands, and safety rules (no raw secrets — read keys from `~/.hermes/.env`; redact in output).
   - Offer to also land it in the `jinwon-int/ccc-node` template (`claude/skills/`) via PR, and record it in the Wiki (use the `wiki-record` skill).

## Rules
- Never author or overwrite a skill without explicit approval.
- Keep skills node-agnostic where possible; keep secrets out (locations/handling only).
- The scan is a heuristic over command *shapes* — always sanity-check that a candidate is a real recurring procedure before proposing.
