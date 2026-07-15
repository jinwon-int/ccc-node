# Shared-all memory autoresearch program

Improve the synthetic memory ranking policy while preserving conversation and
owner boundaries.

## Editable surface

- Edit only `candidate.py`.
- Do not edit the evaluator, fixtures, production memory implementation, tests,
  or configuration during an experiment.
- Use only the synthetic fixtures in this directory. Do not read real Telegram
  messages, Wiki caches, Honcho state, credentials, or network resources.

## Bounded experiment loop

1. Run `python3 research/memory/evaluate.py --summary`.
2. Inspect the full JSON evidence for failing cases.
3. Make one small ranking hypothesis in `candidate.py`.
4. Keep it only when the scalar score improves and contamination avoidance does
   not regress. Otherwise restore the last best candidate.
5. Stop after the experiment count supplied by the operator.

The score weights recall at 40%, precision across the returned context at 30%,
owner/secret contamination avoidance at 20%, and context-budget adherence at
10%. Precision at rank one is also reported as diagnostic evidence.

`shared-all` means relevant DM and invited-group memory may be retrieved for the
same owner. It never means crossing owner boundaries or retrieving secret
material. An improved candidate is evidence only; promotion to production still
requires normal tests, exact-tree review, CI, and human approval.
