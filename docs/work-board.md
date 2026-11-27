# Fleet work board (#2914)

One auto-generated Wiki page lists every open issue and pull request of the
organisation, sorted by whose turn it is. It replaces hand-made snapshots of
"what is still open", which went stale the day they were written.

## Pieces

| File | Role |
|---|---|
| `scripts/ccc-work-board.py` | collect (GitHub GraphQL, read-only) → render (Markdown) → publish (`wiki-agent write-path` → page → `wiki-agent pr`) |
| `scripts/ccc_issue_status.py` | the `status:v1` marker: validate, format, parse; `set` posts a marker comment, `show` prints an issue's current state |
| `scripts/install-work-board-timer.sh` | systemd service + timer on the hub node (dry-run by default) |

## The page

`pages/current/work-board.md` in the Wiki, rewritten once a day (05:30 KST by
default). Sections: summary counts, owner's turn, waiting for a date (by due
date), node's turn, waiting on an outside party, overdue, invalid marker,
unclassified (oldest activity first), issues closed in the last 7 days
(newest first), open PRs, per node, per repository.

What it carries: repository, number, title, labels, dates, and the fields of
the latest `status:v1` and `issue-claim:v1` markers. Comment bodies are read
only to find markers and are never stored or rendered. A closed issue carries
only repository, number, title and closing date; completed and "not planned"
closes are both listed, and no comment of a closed issue is read. Issues opened outside
the fleet appear by number only, and only fleet authors' comments can set a
status. Titles are HTML-escaped and `|` is escaped for the table.

The page is regenerated in full; edits by hand are overwritten. When nothing
but the generation time changed, no Wiki PR is opened. The page's `DOC-` id is
allocated once (max id + 1) and kept on later runs.

The repository list is read from the API at run time, so no repository name is
written in this public repository.

## The marker

```
<!-- status:v1 waiting=owner|node|date|external node=<node> due=YYYY-MM-DD[THH:MM] next="<one line>" -->
```

| field | meaning |
|---|---|
| `waiting=owner` | the owner's approval, decision, check or own work |
| `waiting=node` | a node's turn (including work in progress) |
| `waiting=date` | runs at a set time — `due` is required |
| `waiting=external` | an outside party: a store review, a support reply, a vendor |
| `node` | the node that owns the next step |
| `due` | KST date or date-time |
| `next` | the next step in Korean, one line, at most 80 characters — never an owner's message or a comment body |

- The marker is the first line of a comment, like the issue-claim marker.
- The newest valid marker on an issue is its state. A newer invalid marker is
  shown under "invalid marker" until a valid one replaces it.
- A status whose `due` is more than one day past shows as "overdue".
- No marker (or only a released claim) shows as "unclassified".
- Only the last 40 comments of an issue are read.

Post one with the helper rather than by hand:

```bash
python3 scripts/ccc_issue_status.py set --repo OWNER/REPO --issue N \
  --waiting date --due 2026-11-29T20:00 --node <node> --next "잠금 화면 측정 세션"
python3 scripts/ccc_issue_status.py show --repo OWNER/REPO --issue N
```

`gh-pr-flow` (approval requests) and `bridge-safe-detached-run` (scheduled
checks) tell agents when to leave one.

## Install (hub node)

```bash
scripts/install-work-board-timer.sh --org <org>            # dry-run: prints the units
scripts/install-work-board-timer.sh --org <org> --apply    # writes and enables them
scripts/install-work-board-timer.sh --uninstall --apply    # removes them (page stays)
```

The unit needs the account's HOME: the `gh` login that can read the
organisation and the `wiki-agent` PR worktree. Each run writes one result line
to `<state>/ccc-work-board.result`; with the detached-job notifier installed, a
success goes to the morning digest and a failure is sent at once. A run that
finds another run holding `<state>/work-board.lock` exits 1.

## Not covered

- Work without an issue (a promise made only in chat) does not appear. Open an
  issue for anything that should be on the board.
- Scheduled checks on the nodes are not collected yet (planned as a later step
  of #2914).
