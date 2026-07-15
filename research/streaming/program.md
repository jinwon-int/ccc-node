# Streaming boundary autoresearch program

Your job is to improve Telegram semantic bubble boundaries using the local,
synthetic evaluator in this directory.

## Editable surface

- You may edit only `candidate.py`.
- Do not edit `evaluate.py`, `fixtures.json`, repository tests, production bridge
  code, configuration, or documentation during an experiment.
- Do not read local Telegram transcripts, credentials, caches, home-directory
  state, or network resources.

## Experiment loop

1. Run the baseline:

   ```bash
   python3 research/streaming/evaluate.py --summary
   ```

2. Inspect the full JSON evidence when a case fails:

   ```bash
   python3 research/streaming/evaluate.py
   ```

3. Make one small hypothesis-driven change to `candidate.py`.
4. Re-run the evaluator.
5. Keep the change only when the score improves without introducing an invalid
   case. Otherwise restore `candidate.py` to the last best version.
6. Record the hypothesis, candidate SHA-256, score, and per-metric delta in the
   experiment log supplied by the operator. Never place generated logs in Git.

Stop after the operator's bounded experiment count. Do not run indefinitely.

## Objective

Maximize the scalar score while preserving all safety invariants:

- user-visible text is neither invented, omitted, nor duplicated;
- a terminal answer is not emitted as an interim message;
- a completed progress message is released before later tool work or text;
- private reasoning and tool-only events never become user-visible bubbles;
- no bubble exceeds Telegram's 4,000-character safety ceiling.

The evaluator weights exact ordered bubble sequences most heavily, then content
precision/recall, then release latency. A candidate contract violation makes the
whole run invalid.

An improved candidate is research evidence only. Production promotion requires
normal bridge tests, exact-tree review, CI, and human approval.
