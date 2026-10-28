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

Each row also carries `shadow_would` (`delegate` for L1/L2, `escalate` for L3),
`human_decision: approve`, and `agreement`. "상향 오판" (an L3 the owner would
have delegated) cannot be derived automatically; it is judged by hand from the
rows during the Q5 review.

## Review (Q5)

```bash
python3 ~/.codex/skills/gh-pr-flow/scripts/delegated_approval_shadow.py summary --days 14
# → total, by_tier, by_rule, agreement_rate (= L1+L2 share), escalation_rate, repos, first/last
```

Per node (each node approves from its own checkout); the hub aggregates by
collecting the JSON over ssh when the window closes. The Phase 1 window is
**14 days from the merge of the recorder PR** (Q5); Phase 2 (real L1
delegation) needs agreement ≥ 95 % **and** zero 상향 오판, plus the Phase 2
prerequisites in the spec (§6, Q7, Q10) — none of which this recorder changes.
The classification fixture (Q11) is a separate PR fed by these rows.
