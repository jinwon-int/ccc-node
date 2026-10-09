- **nunchi: SessionStart constraint block gets its own budget (#2216).** On a
  node with 1,221 open constraints the assembled block was 169 KB (≈40k tokens)
  per session while facts stayed capped at 3,000 B. `assemble` now bounds the
  constraint block to `CCC_NUNCHI_CONSTRAINT_BUDGET` bytes (default 12000; `0`
  restores the unbounded G4 behaviour): hint-matched constraints first, then
  newest; near-duplicates (same normalised first 40 chars) fold into one line
  with `(+N 유사)`; a tail line counts what was left out and points at the new
  `nunchi.py constraints` listing. Measured on yukson: 169 KB / 1,227 lines →
  12.8 KB / 68 lines.
