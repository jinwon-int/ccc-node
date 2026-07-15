# Streaming boundary autoresearch pilot

This is a local, synthetic adaptation of Karpathy's autoresearch pattern for
ccc-node's Telegram semantic-message boundaries. It deliberately separates the
agent-editable candidate from the fixed evaluator and fixtures.

```text
candidate.py   agent-editable policy; never imported by production
evaluate.py    fixed deterministic scorer
fixtures.json  synthetic Claude/Codex event traces and expected bubbles
program.md     bounded agent instructions and safety constraints
```

Run the baseline from the repository root:

```bash
python3 research/streaming/evaluate.py --summary
python3 research/streaming/evaluate.py --fail-under 100
```

The result is deterministic JSON with a 0-100 scalar score and case-level
evidence. The fixtures contain no real conversations, secrets, network calls,
or production Telegram sends.

This directory is not a deployment path. An experiment may edit only
`candidate.py`; a selected candidate must be manually translated into a small
production patch and pass the repository's normal review and rollout gates.
