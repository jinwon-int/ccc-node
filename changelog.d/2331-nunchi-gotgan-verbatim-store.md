- **feat(nunchi): `gotgan` — nunchi's own verbatim transcript store, opt-in beside MemPalace (#2331 P1).**
  `claude/hooks/nunchi/gotgan.py` indexes Claude/Codex/Piri JSONL into one
  owner-only SQLite file (`$NUNCHI_HOME/gotgan.db`) with stdlib `sqlite3` only:
  an FTS5 `trigram` index for identifiers and 3+ character terms, plus a Hangul
  bigram FTS5 index matched as a phrase so 2-syllable Korean words (which a
  trigram index cannot see) still hit. Tool calls/results, hidden reasoning,
  `<system-reminder>` blocks and nunchi's own Codex feed prompt are never
  indexed; indexing is append-only by byte offset, re-indexes rotated files, and
  skips symlinks/foreign-owned files. Optional model2vec static embeddings are
  fused with reciprocal-rank fusion when installed; otherwise search is
  lexical-only. `nunchi.py` gains `verbatim_excerpts()` with
  `CCC_NUNCHI_VERBATIM_BACKEND=mempalace|gotgan|both` — the default stays
  `mempalace`, so live behavior does not change until a node opts in. No cron or
  setup wiring yet (P3).
