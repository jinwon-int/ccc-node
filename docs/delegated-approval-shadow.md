# Delegated-approval shadow recorder (Phase 1)

a2a-nexus#2317 ratified the fleet's 하부위임전결 matrix (L0–L3,
`docs/specs/delegated-approval-matrix.md` in a2a-nexus). Before any approval is
delegated, **Phase 1 only measures**: for every cross-account PR approval a
person actually gives, record which tier the matrix *would* have assigned and
compare. The owner placed the recorder here (2026-10-23) because the real
decision point is `codex/skills/gh-pr-flow/scripts/approve-via-relay.sh`, not
the broker (whose approval flow had zero live uses). Tracking:
jinwon-int/ccc-node#2137.

## What it does — and does not

- `approve-via-relay.sh` runs the relay-held review exactly as before. After a
  **real, successful** approval it calls
  `delegated_approval_shadow.py record …` (same directory, best-effort). The
  recorder appends one line to `~/.claude/state/delegated-approval/shadow.jsonl`
  (0600, dir 0700) and prints one line to **stderr**. Stdout stays the
  approval JSON; a recorder failure never changes or fails the approval.
- It never decides anything, never calls the broker, never reads PR bodies or
  review text (principle 4: structured evidence only — paths, diff totals,
  issue refs from the title — bare `#N` is this repo, `repo#N` shares its owner,
  `owner/repo#N` is kept as written — author/approver, base).
- Off switch: `CCC_DELEGATED_SHADOW=0`. The ratified kill/enable switches
  (`A2A_DELEGATED_APPROVAL_KILL`, `A2A_DELEGATED_APPROVAL`) are only
  **snapshotted** into each row; Phase 1 has no behaviour to switch.

## Tier rules (matrix v0, fail-closed)

| tier | rule_id | when |
|---|---|---|
| L3 | `v0.L3.self-approval` | author == approver |
| L3 | `v0.L3.no-evidence` | no changed files reported |
| L3 | `v0.L3.path` | any path: `.github/workflows/**`, `.github/required-checks.json`, `secret(s)` / `.env*` / token·credential·api-key·password file names, `release*`/`publish*` scripts or workflows, `deploy/**`, the matrix spec itself |
| L3 | `v0.L3.promotion-branch` | approval targeted a promotion branch (`--base`) |
| L3 | `v0.L3.q2-count` / `v0.L3.q2-diff` | Q2 anti-split: ≥5 prior approvals in 24h for the same repo + issue ref, or cumulative diff > 800 lines |
| L1 | `v0.L1.docs-test-only` | every path is docs (`docs/`, `*.md`, `changelog.d/`, `CHANGELOG*`, `README*`, `LICENSE*`, `pages/`) or tests (`*_test.py`, `*.test.sh`, `*.test.mjs`, `tests/`, `fixtures/`, `test_*.py`) |
| L2 | `v0.L2.code` | anything else (non-live repo code PR merge) |

Each approval row also carries `shadow_would` (`delegate` for L1/L2, `escalate`
for L3), `human_decision: approve`, and `agreement`.

## Requests and refusals (spec §10.2 5-d, schema v2)

An approval row exists only when a person approved, so a refusal left nothing
behind. The spec's "상향 오판" is **a shadow L1 the person refused**; with
approvals alone it could not be counted (the Phase 1 window had 548 approval
rows and no way to see a refusal). Two more row kinds record the other half.
They are record-only too: nothing here changes whether an approval is submitted.

| Row | Written by | Meaning |
|---|---|---|
| `kind: ask` | `delegated_approval_shadow.py ask --repo … --pr … --head …` | an agent asked the owner to approve this PR at this exact head. Same evidence and tier as an approval row, no approver |
| `kind: decision` | `… decide --repo … --pr … --decision … --source …` | a person's non-approval for that head: `decline` (chose another option), `hold`, `closed-unmerged`. `reply-unclassified` only notes that an answer could not be classified |
| no `kind` / `kind: approve` | `record` (unchanged call from `approve-via-relay.sh`) | the approval. v2 rows add `implicit_ask: true` when no `ask` was recorded for that head |

- **No text is stored.** A decision row holds the class, the source
  (`reply` · `github` · `manual`), the PR and the head. `--detail` accepts a
  short class label (`[a-z0-9-]`), not a sentence. A session is kept as a
  12-character digest.
- **How an ask ended is derived in `summary`, not written**, so it does not
  depend on write order or on a sweep job: the first approval or terminal
  decision on the same head wins; a later ask or approval for the same PR at
  another head means `superseded`; no answer for 24 h (the receipt validity,
  Q4) means `expired`; otherwise `open`. `expired` is not a refusal (owner
  decision 2026-11-12).
- `ask`, `decide` and `record` are best-effort: quiet on stdout, exit 0, one
  line on stderr. `CCC_DELEGATED_SHADOW=0` turns all three off.

Not yet: the `gh-pr-flow` step that calls `ask` and the reply hook that
classifies the owner's answer. Until they land, `ask` and `decide` are called
by hand or not at all, and `implicit_ask` shows how many approvals had no
recorded request.

## Collect and reconcile (window verdict helpers)

`delegated_approval_reconcile.py` sits next to the recorder. Both commands only
read: one `cat` per node, `gh` list/view calls. They never write a ledger row.

```bash
R=~/.codex/skills/gh-pr-flow/scripts/delegated_approval_reconcile.py
# 1) every node's ledger into one directory (nodes from --node or CCC_FLEET_NODES;
#    CCC_FLEET_SELF is read locally; CCC_FLEET_SSH overrides the ssh command)
python3 "$R" collect --out ./ledgers
# → nodes{status ok|no-ledger|unreachable, rows, dropped}, without_recorder, unreachable, complete
# 2) what GitHub shows for the same window, against those ledgers
python3 "$R" reconcile --since 2026-10-28T04:22:00+09:00 --until 2026-11-11T04:22:00+09:00 \
  $(for f in ./ledgers/*.jsonl; do printf -- '--ledger %s ' "$f"; done)
# → closed{merged, unmerged}, by_tier, l1{total, merged, closed_unmerged, changes_requested,
#   hold_comment, reverted, refused[], approved_as_asked_rate}, coverage{…}, errors, complete
```

- `reconcile` lists every PR merged or closed-unmerged in `[since, until)` in
  the given repositories (default: the repositories in the ledgers) and
  classifies each with the recorder's own path and size rules. A PR on a base
  other than `main`/`master` counts as L3 (promotion branch).
- For the **L1** ones it looks for a refusal a person left on GitHub: closed
  without merging, a `CHANGES_REQUESTED` review, a comment that starts with
  `⛔ hold` (the hold channel in spec Q1), and a merged `Revert …` PR that names
  it within seven days. `l1.approved_as_asked_rate` is spec §10.2 **5-b**;
  `l1.refused` and `l1.reverted_prs` feed **5-c** together with
  `summary`'s `asks.upward_misjudgement`.
- `coverage` says how much of what merged the ledgers saw: `outside_ledger`
  (merged without a recorded approval), `outside_by_bot`, `l1_outside_ledger`.
  `collect` names the nodes without a recorder. Both go into the verdict as
  counts (spec §10.2 "측정 방법").
- A repository that fails after three tries is listed in `errors`, adds nothing
  to the totals, and makes the exit code 1 — a partial report is never passed
  off as complete. Same for an unreachable node in `collect`.
- Nothing textual is kept: titles are read only for the PR number a revert
  names, reviews for their state, comments for the marker.

Run on 2026-11-12 over the Phase 1 window: 12 nodes collected (one without a
recorder), 630 merged and 3 closed-unmerged PRs in 16 repositories, 53 L1, none
refused or reverted, 87 merges outside the ledgers (13 by bots, 10 of them L1).

## Review (Q5)

```bash
S=~/.codex/skills/gh-pr-flow/scripts/delegated_approval_shadow.py
python3 "$S" summary --days 14          # this node, counted back from now
python3 "$S" summary --since 2026-10-28T04:22:00+09:00 --until 2026-11-11T04:22:00+09:00 \
  --ledger nodes/a.jsonl --ledger nodes/b.jsonl   # a fixed window over collected ledgers
# → total, by_tier, by_rule, agreement_rate (= L1+L2 share), escalation_rate, repos, first/last,
#   asks{total, by_resolution, l1, upward_misjudgement, reply_unclassified}, implicit_ask{…}
```

A ledger is per node (each node approves from its own checkout). **A window
verdict uses `--since/--until` over every node's ledger**; `--days N` on one
node counts back from the moment it runs and is not a verdict (spec §10.2
"측정 방법"). Window edges need an explicit UTC offset.

The Phase 1 window ran 2026-10-28 04:22 → 2026-11-11 04:22 KST. Its
`agreement_rate` was 0.6734, which says how much of the approved work the
matrix would have delegated, not how often it was wrong: L3 is the person's by
design. The spec was amended on 2026-11-12 (§10.2, a2a-nexus#2549): Phase 2
(real L1 delegation) now needs **5-a** zero misclassified L1, **5-b** L1
approved-as-asked ≥ 95 % with at least 30 samples, **5-c** zero 상향 오판
(`asks.upward_misjudgement`, plus reverts within 7 days), and **5-d** this
refusal path implemented and verified — plus the prerequisites in the spec
(§6, Q7, Q10), none of which this recorder changes. `agreement_rate` and
`escalation_rate` stay in the report but are no longer the gate. The
classification fixture (Q11) is a separate PR fed by these rows.
