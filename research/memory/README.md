# Shared-all memory autoresearch track

This local synthetic track evaluates candidate ranking policies for ccc-node's
shared-all memory mode. It covers current-versus-historical facts, same-group
preference, explicit cross-group recall, relevant DM recall, global policy,
owner isolation, secret exclusion, and a bounded context budget.

```bash
python3 research/memory/evaluate.py --summary
python3 research/memory/evaluate.py --fail-under 100
```

The candidate is not imported by production. Real conversations and caches are
out of scope. A selected ranking change must be manually translated into a
reviewable production patch after it wins against a larger approved fixture set.
