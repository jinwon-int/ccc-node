---
name: skillsuggest
description: Detect repeated procedures and review create, patch, or support-file skill proposals (human-in-the-loop), and audit or roll back autosave auto-mode changes. Use when asked to find automatable routines, improve an existing skill, review skill candidates, or list and roll back autosave-installed skills.
---

# skillsuggest — propose skills from repeated work (+ autosave post-hoc review)

Approximates "auto-skillification": find procedures you keep repeating and
either improve the matching skill or create a distinct one. Detection is
automatic; **anything this skill authors or applies requires user approval**.
Skill Review may stage legacy `SKILL.md` drafts or isolated v2
`proposal.json` create/patch/write_file packages under the pending queue.

Two autosave modes change what "review" means here (`docs/skill-autosave.md`):
- **approve** (default): drafts wait in the pending queue; this skill is the approval gate (step 1 below).
- **auto** (#355, opt-in): machine gates install passing drafts unattended and the owner is notified after the fact; this skill becomes the **post-hoc review / rollback** tool (step 1b below). Drafts that failed a gate stay pending and are still approved/rejected via step 1.

## Procedure

1. **Check pending Skill Review drafts first** (LLM-drafted, still human-gated):
   The queue is node-global. Resolve it with `CCC_SKILL_REVIEW_STATE_DIR`, never
   with `CCC_STATE_DIR` — the bridge scopes that one per memory audience, and
   reading it here reports an empty queue while real drafts sit unread.
   ```bash
   STATE="${CCC_SKILL_REVIEW_STATE_DIR:-$HOME/.claude/state}"
   find "$STATE/pending-skills" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort
   ```
   For a chosen `<id>`:
   ```bash
   STATE="${CCC_SKILL_REVIEW_STATE_DIR:-$HOME/.claude/state}"
   jq . "$STATE/pending-skills/<id>/meta.json"
   AUTO="${CCC_CLAUDE_DIR:-$HOME/.claude}/hooks/skill-review/autoinstall.sh"
   if [ -f "$STATE/pending-skills/<id>/proposal.json" ]; then
     bash "$AUTO" render <id>
   else
     sed -n '1,220p' "$STATE/pending-skills/<id>/SKILL.md"
   fi
   ```
   For a v2 proposal, approve only after checking its exact target, expected
   hashes, provenance and full diff/content. Never copy v2 content by hand:
   ```bash
   bash "$AUTO" apply <id>
   ```
   For a legacy `SKILL.md` draft, approve only after reading the full draft and
   checking for node-specific facts or secrets:
   ```bash
   STATE="${CCC_SKILL_REVIEW_STATE_DIR:-$HOME/.claude/state}"
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
   STATE="${CCC_SKILL_REVIEW_STATE_DIR:-$HOME/.claude/state}"
   mv "$STATE/pending-skills/<id>" "$STATE/pending-skills/<id>.rejected-$(date -u +%Y%m%d%H%M%S)"
   ```
   A pending draft with an `autosave-block.json` file was machine-rejected by
   the auto mode gate — `jq . "$STATE/pending-skills/<id>/autosave-block.json"`
   shows the reason (secret / node-specific / lint / dedup / codex-incompat /
   **unverified-claim** / **dead-citation**). Give those extra scrutiny before
   approving.

   The last two come from `gate_unverified_claims` and mean the draft states a
   falsifiable technical fact the reader cannot re-derive:
   - `unverified-claim exit-code|http-status|version-pin` — the draft asserts an
     exit code, HTTP status, or pinned version with **no citation**: no URL, no
     `file.ext:line` source reference, no `--help`/`--version` command shown,
     and no dated "verified" marker. Do not approve by judging whether the claim
     *sounds* right. Re-derive it: run the command, or read the source, then
     require the draft to carry that evidence before approval.
   - `dead-citation http-404|http-410` — a URL the draft cites is gone. Find the
     live path and correct the citation; a dead citation is indistinguishable
     from a fabricated one to the next reader.

   The gate checks **citability, not truth** — a machine cannot verify a claim
   is correct, only that it is checkable. Approving one of these means *you*
   became the verification step; actually do the check.

1b. **Autosave post-hoc review / rollback** (auto mode installs are tracked in a
   ledger and always reversible):
   ```bash
   AUTO="${CCC_CLAUDE_DIR:-$HOME/.claude}/hooks/skill-review/autoinstall.sh"
   bash "$AUTO" list              # ledger + currently installed + blocked drafts
   bash "$AUTO" status            # mode, daily cap usage, recent activity
   bash "$AUTO" rollback <name>   # archive one auto-installed skill (undo)
   bash "$AUTO" rollback --all    # bulk-undo every auto-installed skill
   ```
   Rollback archives into `~/.claude/state/skill-autosave-rollback/` (never
   deletes) and refuses skills that lack the `.autosave-meta.json` marker, so
   hand-authored skills are untouchable. Mode switch (owner decision — ask
   before changing it): `printf auto > "$STATE/skill-autosave.mode"` or export
   `CCC_SKILL_AUTOSAVE_MODE=auto`; remove/`approve` to restore the human gate.

2. **Refresh deterministic candidates** (command-shape scan of transcripts):
   ```bash
   bash ~/.claude/skills/skillsuggest/scan.sh
   cat ~/.claude/state/skill-candidates.md
   ```
   (The daily skill-autosave sweep also refreshes this file and drafts skills
   from bridge/SDK transcripts — enable it with
   `scripts/install-skill-autosave-cron.sh --apply`; see `docs/skill-autosave.md`.)

3. **Interpret, don't dump.** Read the ranked command shapes and cluster them into *procedures*:
   - Group related high-count shapes by tool/intent (e.g. `wiki-agent write-path` + `wiki-agent pr` = the wiki flow).
   - Drop shapes already covered by an existing skill (listed in the report).
   - Drop one-off/trivial shapes; a skill is worth it only for a multi-step procedure you repeat across sessions.

4. **Propose (max ~3).** For each strong candidate, show the user a short proposal: name, one-line description, the steps it would encode, and why it recurs. Ask for approval (numbered options).

4b. **Pending-draft reviews use the full review format.** When presenting blocked/pending drafts for a decision, show per draft: **배경** (origin session id/date + the task that produced it — read the source transcript's opening user messages when available), **제안 이유** (what recurring pain/procedure it encodes), **차단 사유·정당성** (the gate reason and whether the gate was right), **개선사항** (numbered, concrete: what must change before approval — heading format, trigger-style description, node-specific path generalization, session-specific numbers), and a **판정** (approve-after-fix / reject-and-why / hold). Then a single numbered approval request.

5. **Author on approval only.** For each approved deterministic candidate, create `~/.claude/skills/<name>/SKILL.md`:
   - Frontmatter: `name` (kebab-case) + a detailed `description` (it drives auto-matching).
   - Body: numbered steps, exact commands, and safety rules (no raw secrets — read keys from `~/.hermes/.env`; redact in output).
   - Offer to also land it in the `jinwon-int/ccc-node` template (`claude/skills/`) via PR, and record it in the Wiki (use the `wiki-record` skill).
   - When landing a new `claude/skills/<name>/` in the template, also add its classification entry to `codex/compatibility.json` (`adapted` with a codex mirror, or `claude-only` with a reason). The catalog is fail-closed: an unclassified skill fails `validate-harness` (`catalog_unclassified`) and blocks CI (#789).

## Rules
- Never author or overwrite a skill without explicit approval. (In auto mode the
  *pipeline* installs machine-gated drafts on its own — but you, running this
  skill, still never install, roll back, or change the mode without the user's OK.)
- Keep skills node-agnostic where possible; keep secrets out (locations/handling only).
- The scan is a heuristic over command *shapes* — always sanity-check that a candidate is a real recurring procedure before proposing.
- Treat rollback as safe and cheap: when the user doubts an auto-installed skill, roll it back first and re-propose later.
