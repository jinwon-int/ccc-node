---
name: self-update
description: Safely update this node's ccc-node harness (~/.claude) to GitHub latest — detect drift, show the diff, back up, run setup.sh, validate, and roll back on failure. Use when asked to update/upgrade the harness, sync a node to the latest ccc-node, or check harness drift across the fleet. Approval-gated; detection is read-only and applying never happens without explicit OK. Not for the Telegram bridge (use bridge/start.sh --upgrade).
---

# self-update — safe harness update (fleet drift control)

Brings a node's installed Claude Code harness (`~/.claude`) up to date with the
canonical `jinwon-int/ccc-node` repo. **Detection is read-only and automatic;
applying requires explicit approval, always backs up first, validates after, and
has a rollback path.**

## What is and isn't touched

`setup.sh` only overwrites **node-agnostic** harness files and **preserves node
identity**:

- **Overwritten** (common harness): `settings.json`,
  `hooks/*.sh`, `agents/`, `commands/`, `skills/`, `output-styles/`, `headless.sh`.
- **Preserved** (seeded only if absent — never overwritten): `settings.local.json`
  (node-local approvals, seeded from `settings.local.template.json`; #454),
  `CLAUDE.md`, `memories/MEMORY.md`, `memories/USER.md`, `~/.hermes/honcho.json`,
  `hooks/tools-cheatsheet.md`.
- The old `~/.claude` is snapshotted to `~/.claude/backups/ccc-node-setup-<ts>.tar.gz`
  before anything is written (unless `--no-backup`, which self-update must NOT use).

> Caveat: `settings.json` IS overwritten. If this node has local settings.json
> customizations, confirm they survive (or re-apply from the backup)
> after updating.

## Procedure

1. **Detect drift** (read-only — fetches, changes nothing):
   ```bash
   bash ~/.claude/skills/self-update/check.sh
   ```
   Reports up-to-date / N commits behind, the new commits, changed harness files,
   and the CHANGELOG delta. If it says "up to date", stop here.

2. **Report & get approval.** Summarize the pending commits + CHANGELOG and ask the
   operator with numbered options. Do not proceed without an explicit yes.

3. **Pull the repo** (after approval):
   ```bash
   git -C /opt/ccc-node pull --ff-only
   ```

4. **Preview the install** (changes nothing):
   ```bash
   /opt/ccc-node/setup.sh --dry-run
   ```
   Report what would be written.

5. **Apply** (second approval — overwrites the common harness; setup.sh snapshots
   the old config first):
   ```bash
   /opt/ccc-node/setup.sh
   ```

6. **Validate**:
   ```bash
   bash /opt/ccc-node/scripts/validate-harness.sh
   ```
   Then advise starting a fresh session to confirm hook/memory injection before
   declaring success (cf. the evidence gate).

7. **Roll back on failure** — restore the snapshot and reset the repo:
   ```bash
   ls -t ~/.claude/backups/ccc-node-setup-*.tar.gz | head -1   # newest snapshot
   # tar -xzf <newest-snapshot> -C ~/.claude                   # restore harness
   # git -C /opt/ccc-node reset --hard <previous-sha>          # restore repo
   ```

## Rules
- `check.sh` is safe anytime; **pull / setup.sh / any restart need approval**
  (Fresh-Approval: harness mutation).
- Never pass `--no-backup` during self-update — the snapshot is the rollback path.
- After applying, run validate-harness and confirm a clean session before declaring
  success.
- Bridge code (`/opt/ccc-node/bridge`) is out of scope — update it separately via
  `bridge/start.sh --upgrade` (its own approval-gated step).
