# Issue #143 A2A roadmap matrix

This document turns the broad repo-evaluation issue into explicit follow-up
tracks without claiming every recommendation is already implemented.

- Issue: <https://github.com/jinwon-int/ccc-node/issues/143>
- Finalizer: `seoseo-finalizer`
- A2A broker: Seoseo Team1 (`brokerId=seoseo`)
- Initial round: `a2a-ccc-node-143-review-20260623T233208Z`
- Source-backed retry: `a2a-ccc-node-143-source-review-20260623T233208Z`
- Repository head reviewed locally: `9f428c272202e137bdff7830660a07455b23ce5f`

## A2A evidence summary

| Task id suffix | Worker | State | Counted as substantive? | Notes |
|---|---|---:|---:|---|
| `:nosuk-roadmap` | `nosuk` | `succeeded` | Yes | Recommended keeping #143 as a roadmap/status umbrella, splitting P1 items into child trackers, and using small docs/CI PRs for P2/P3 work. |
| `:sogyo-implementation-slices` | `sogyo` | `failed` | No | Handler exited with `invalid Hermes analysis JSON schema`; not used for consensus. |
| `:yukson-verification-closeout` | `yukson` | `succeeded` / blocked | No | Correctly refused closeout from insufficient source evidence. |
| `:nosuk-pr-slice` | `nosuk` | `succeeded` / blocked | No | Source bundle was too truncated for a safe PR-slice recommendation. |
| `:yukson-closeout-matrix` | `yukson` | `succeeded` / blocked | No | Source bundle did not arrive in usable form for the Hermes bridge lane. |

Finalizer note: blocked lanes are useful negative evidence. They prevented an
unsafe “issue closed” decision from partial or truncated context.

## Status matrix

| Priority | Topic | Decision | Rationale / acceptance criteria |
|---|---|---|---|
| P1 | Move `scripts/agent-cron.sh` from Bash to Python | Child tracker required | The current Bash implementation is stateful and large. A follow-up must preserve the existing CLI contract, lock/retry/run-history behavior, dry-run defaults, and `scripts/agent-cron.test.sh` coverage before replacing the shell path. |
| P1 | First-class non-root install paths | Child tracker required | This touches `setup.sh`, bridge startup defaults, state/cache paths, wiki-agent path assumptions, and secret-location rules. Acceptance must include root and non-root dry-run/install validation without printing or moving secrets. |
| P2 | CI matrix | Backlog / small PR candidate | Current CI uses one workflow on `ubuntu-latest` and Python 3.12 for bridge tests. A safe slice can add an explicit Ubuntu/Python matrix if CI cost and runtime are acceptable. |
| P2 | Bridge upstream tracking policy | Backlog / small docs PR candidate | README says the upstream relationship was intentionally dropped. A docs-only follow-up should define how security fixes or upstream advisories are noticed, evaluated, and either ignored or ported. |
| P3 | README split | Backlog / small docs PR candidate | README is intentionally comprehensive but heavy. Split only if it improves operator onboarding without hiding the secret-handling and single-owner hook warnings. |
| P3 | Bash test runner / failure logs | Backlog | `validate-harness.sh` currently prints bounded failure context. A safer slice should improve diagnostics while keeping CI noise controlled. |
| P3 | Bridge README canonical/i18n policy | Backlog | `bridge/README.md` and `bridge/README-zh.md` both exist. A docs-only follow-up can state which one is canonical and how translations are maintained. |

## Closeout policy for #143

Do not close #143 solely because this roadmap exists. Close it only after one of
these happens:

1. child trackers for both P1 items exist and are linked from #143, with explicit
   acceptance criteria; or
2. the owner decides #143 should remain a historical evaluation note and closes it
   as superseded by this roadmap plus narrower follow-ups.

No deploy, broker/worker restart, provider canary, DB migration, ACK/replay,
release, or secret movement was performed during this A2A pass.
