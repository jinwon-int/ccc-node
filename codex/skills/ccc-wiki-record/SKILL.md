---
name: ccc-wiki-record
description: Record durable operating knowledge in the Seoyoon Family Wiki through the wiki-agent PR-first workflow. Use for decisions, runbooks, node facts, incidents, task memory, or operating-log entries that must survive the current session without storing raw secrets.
---

# CCC Wiki Record

1. Read before writing:

   ```bash
   wiki-agent find "<topic>"
   wiki-agent load <candidate-path>
   wiki-agent write-path
   ```

2. Edit only the returned PR worktree. Route node facts to
   `pages/nodes/<node>/`, agent work to `pages/team/<node>/`, people/account
   responsibility to `pages/owners/`, and reusable procedures or decisions to
   the matching runbook/decision page.

3. For new operating-log entries, use
   `## [LOG-YYYYMMDD-<node>-<same-day-sequence>] YYYY-MM-DD KST — <title>` as a
   level-2 heading and prepend it at the top of the log page, above the newest
   existing heading entry. The bullet region below the `[LOG-00]` block is the
   older form; both are auto-merged and share one id space, so count either when
   picking the sequence. Never edit the `[LOG-00]` block itself, and never
   allocate or renumber a global numeric `LOG-NNNN`.

4. Record secret locations and handling rules only. Exclude tokens, cookies,
   private keys, message bodies, credential values, and private endpoints.

5. Review the exact diff, then submit:

   ```bash
   git -C "$HOME/.wiki-agent/wiki-pr-work/seoyoon-family-wiki" diff --check
   wiki-agent pr
   ```

6. Confirm the PR and merge result. Report the new durable IDs and links.
