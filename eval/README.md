# eval/ — fleet evaluation gate (observation)

The Q-set, runner and gate live in
[jinwon-int/agent-olympics](https://github.com/jinwon-int/agent-olympics)
(`docs/fleet-eval.md`, issue #325). This directory only holds what the gate
needs from this repository.

- `fleet-eval.lock.json` — the agent-olympics commit the CI checks out, the lane
  (`claude-code-overlay`) and the **overlay**: the files the target is given,
  read from the PR's git ref (`claude/output-styles/ccc-report.md`,
  `claude/CLAUDE.md.template`).
- `fleet-eval/runs/<runId>.jsonl` — body-free run records (hashes and check
  outcomes, never the model output). Append-only: a PR may add run files, never
  edit or delete one.

## What CI does

`.github/workflows/fleet-eval.yml` (`fleet-eval-gate`) calls no model and uses
no secret. It renders the overlay at the base and at the checked-out commit and
reports one of:

| Status | Meaning |
|---|---|
| `not_applicable` | the overlay files did not change and no run was added |
| `candidate_needed` | an overlay file changed; record a run (below) |
| `stale_run` | the added run was recorded for an older overlay |
| `pass` / `fail` | the gate compared the new run with the newest `main` run for the old overlay |

The job is `continue-on-error` while the gate is observed (agent-olympics#325
slice 4c). It becomes a required check only after two weeks of results (4f).

## Recording a run

Only an operator node calls the model (43 items × 3 attempts, about 15 minutes),
with a node-local helper that is not part of this repository:

```
fleet-eval-pr.sh --repo jinwon-int/ccc-node --pr <n> [--push]
```

It renders the overlay from the PR's merge ref, runs the overlay lane
(`claude -p --safe-mode --append-system-prompt-file <overlay>`, tools, hooks and
MCP off), commits the run file and checks it with the same CI command. `--push`
pushes only when the PR author is the local account; otherwise hand the run file
to the author (pushing to another account's PR leaves nobody able to approve it).
