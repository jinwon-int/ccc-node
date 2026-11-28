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
   level-2 heading and prepend it at the top of the log page,
   below the digest block (never right under the H1), directly above the newest
   existing heading entry. The bullet region below the `[LOG-00]` block is the
   older form; both are auto-merged and share one id space, so count either when
   picking the sequence. Never edit the `[LOG-00]` block itself, and never
   allocate or renumber a global numeric `LOG-NNNN`.
   Locate that heading on a **line start only** (`re.search(r"^## \[LOG-", text, re.M)`).
   The digest quotes the pattern mid-line, so a first-substring match
   (`str.index('## [LOG-')`, `find`, unanchored `sed`) splices the entry into
   the digest — it broke twice on 2026-11-05. Before submitting, the log diff
   must contain only added lines and the digest lines must be unchanged.

4. Record secret locations and handling rules only. Exclude tokens, cookies,
   private keys, message bodies, credential values, and private endpoints.
   When a page states an issue's or PR's status, check it live (`gh issue view`
   / `gh pr view`) and write it with its date (`닫힘 2026-11-28`); never copy it
   from an older page or report, and never change the issue to match the page.

5. Review the exact diff, then submit:

   ```bash
   git -C "$HOME/.wiki-agent/wiki-pr-work/seoyoon-family-wiki" diff --check
   wiki-agent pr
   ```

6. Confirm the PR and merge result (`wiki-agent load pages/log.md | head -40`
   shows the entry as the newest heading). Report the new durable IDs and links.

7. `wiki-agent pr` checks the added lines for secret-like values itself (fixed
   patterns that refuse the PR, plus gitleaks where installed — a warning by
   default). After the push the Wiki PR gate lints the changed pages
   (`wiki-lint --strict`: broken links and anchors, duplicate ids, secret-like
   values); fix and push again when it is red. Never work around a refusal. If a raw credential reached a pushed
   branch or a merged page, do not rotate or revoke it on your own judgement:
   report it by location only (credential, where and since when, owner,
   dependents) and ask its owner for an explicit decision.
