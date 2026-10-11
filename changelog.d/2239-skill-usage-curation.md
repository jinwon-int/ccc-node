- **Skill curation: turning on archive no longer skips the stale mark, fleet-skills installs get their own ownership class, and a read-only usage report covers the fleet (#2239, #1739, #1648).**
  - **Archive opt-in.** With `CCC_SKILL_CURATOR_ARCHIVE_ENABLED=true`, the curator used to move any autosave skill idle for 90 days straight to the archive, skipping the #1739 two-stage rule. It now archives a skill only when all of these hold:
    - the skill is already marked `stale`;
    - at least `RECHECK_AFTER_DAYS` (30) have passed since the mark;
    - the skill has not been used since the mark;
    - it has been idle for at least `ARCHIVE_AFTER_DAYS` (90). This is now a minimum idle time; set it to 60 for the effective 30 + 30 days in #1739.

    Otherwise the curator behaves exactly as mark-only. A use after the mark either reactivates the skill (clearing `stale_marked_at`) or restarts the recheck window. A restored skill starts again at stage 1. The default (archive off) is unchanged.
  - **`fleet-managed` class.** A skill directory carrying the `ccc-fleet-skills-sync.py` marker `.ccc-fleet-skill.json` was classified `user-owned`. It is now `fleet-managed`. The class is reported, never autonomous-writable, adoptable or archived by the local curator, and still pinnable. It ranks above an autosave marker in the same directory. A marker that is not `0600` or belongs to another manager or provider fails closed as `unknown/unreadable`. Fleet retirement still goes through fleet-skills `retired/`.
  - **New `scripts/ccc-skill-usage-report.py`.**
    - `node [--json]` lists each installed skill with its origin (repo-managed, fleet-managed, autosave, external, user, unknown), usage from the curator's ledger union (no double counting), windowed use, curator state and `stale_marked_at`.
    - `aggregate FILE...|-` merges node reports into archive candidates (idle on every node holding the skill), graduation candidates (used on 2 or more nodes, or 3 or more uses, with a dwell check), never-used skills and per-origin totals.
    - Missing or unreadable telemetry never counts as non-use. Those nodes are listed under `evidence_gaps`.
    - The script is read-only and never prints skill bodies.

    See `docs/skill-usage-report.md`.
