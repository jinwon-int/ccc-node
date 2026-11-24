# Nunchi state-aware recall (ccc-node#2464)

`nunchi.py recall` and `dialectic` use `search(..., state_aware=True)`. The constraint-hint
matcher does not (it consumes id sets only). Everything here is LLM-free and falls back to the
plain bm25 order on any failure.

| Layer | What it does | Knob (default) |
|---|---|---|
| (a) recency re-rank | bm25 candidate pool `limit×4` (≥40) re-ordered by `bm25 position + w × recency position` | `NUNCHI_RECENCY_WEIGHT` (0.25; 0 = off). ≥0.5 starts hiding older facts — STALE sweep 2026-10-22 |
| (b) recent-state window | the peer's newest open facts (not `observation`/`task-progress`) are appended regardless of lexical match, up to `min(N, limit//3)` slots | `NUNCHI_STATE_WINDOW` (5; 0 = off); needs a `--target` |
| (c) vectors (optional) | if `fact_vecs` holds model2vec vectors, cosine rank is fused with the lexical order by RRF (k=60). Vectors are written at ingest when `numpy`+`model2vec` import; `nunchi.py reindex-vecs` backfills | same env as gotgan: `GOTGAN_EMBED` (auto/off), `GOTGAN_EMBED_MODEL` (`minishlab/potion-multilingual-128M`) |

`dialectic` prints each current fact with its `valid_from` date and tells the model to treat the
most recent fact on a topic as the current state.

## Why

STALE (arXiv:2605.06527) measured on the pre-change code: implicit conflicts were never caught
at write time (0/400), and a query phrased in the old state's words returned only the old fact
(M_new@5 2%, stale-trap 96%), so the LLM answered the stale state (3/61). The regression bench
`claude/hooks/nunchi/stale-bench.py` (40-scenario CC-BY subset, ratchet `stale-baseline.json`)
tracks three shapes:

| shape | plain bm25 | state-aware (lexical) | + vectors |
|---|---|---|---|
| B pooled (800 facts, one peer, same day) M_new@5 | 1/40 (400: 2.0%) | 4/40 (400: 3.0%) | 10/40 (400: 5.5%) |
| R recent-state (M_old + 40 older distractors + M_new last) both@10 | 15/40 | 38/40 | 37/40 |
| A write-time detection | 0/40 | 0/40 | 0/40 |

The pooled shape is adversarial for recency (every fact is the same age); the recent-state shape
is what a real peer timeline looks like, and there the window makes the current state visible.
Bridging two states that share no vocabulary at write time still needs a semantic signal
(#2464 제안 2); this change only makes the latest state reachable at read time.

## Inflected Korean words in the query (#2885)

`facts_fts` uses FTS5's default tokenizer: it splits on spaces and punctuation, so "배포를" in a
question and "배포" or "배포가" in a fact are different tokens. Every `search()` caller — `recall`,
`dialectic`, `why --query` and the SessionStart hint ranking — therefore sends, next to the query
it always sent, each Hangul word's stem as a prefix query (`"배포"*`):

- the trailing particle is removed with gotgan's rule (`_PARTICLE`), question words in gotgan's
  stoplist and bare particles are skipped, and at most 12 stems are used;
- a stem that would be one syllable is not used; the word itself becomes the prefix ("결과" stays
  "결과", it does not become "결");
- a Hangul ending glued to a non-Hangul word ("PR에서") is one index token (`pr에서`) that no
  Hangul prefix reaches, so the ending is ignored and the head (`"PR"`) is sent as a phrase;
- an alias that only resolves without its ending adds its canonical slug and that slug's hallway
  expansions;
- the old operands (whole words, alias slugs, hallway expansions) are sent first and unchanged, so
  the set of matching facts only grows. The index and stored facts are untouched.

Under a row limit the returned rows are still chosen by bm25 over the larger matching set, so a
row the old query returned can be displaced by a newly matching one (most likely a long fact
holding the exact word, pushed down by short facts repeating an inflected form).

Knob: `NUNCHI_QUERY_PREFIX` (on; `0`/`false`/`off`/`no` = the query as it was).

Measured on one node's fact store, read-only (59 questions written from stored facts; the target
fact among the returned rows):

| path | question style | before | after |
|---|---|---|---|
| plain `search`, 10 rows | keyword | 0.92 (MRR 0.74) | 1.00 (MRR 0.82) |
| plain `search`, 10 rows | natural language | 0.93 (MRR 0.82) | 0.97 (MRR 0.85) |
| `recall` path, 12 rows | keyword | 0.88 (MRR 0.47) | 1.00 (MRR 0.51) |
| `recall` path, 12 rows | natural language | 0.93 (MRR 0.52) | 0.97 (MRR 0.60) |

In this set no target that was returned before dropped out; some moved down a few places (5 of 59
keyword and 8 of 59 natural-language questions on the `recall` path) while more moved up (20 and
24). A character-bigram side index was measured on the same questions and did no better, so none
was added. The questions were generated from the facts themselves, so they share vocabulary with
their targets more than real questions do. Cost: on a synthetic 20,000-fact store the median
query went from about 2 ms to about 13 ms (prefix operands match several times more rows).
`claude/hooks/nunchi/query-prefix.test.sh` pins the behaviour on a synthetic fixture.

## Point-in-time recall, citations and `why` (#2464 제안 3, #2466)

All LLM-free except the one dialectic call that already existed.

| Command | What it does |
|---|---|
| `recall <q> --as-of TS` / `dialectic <q> --as-of TS` / `why --query <q> --as-of TS` | only facts valid at `TS` (`valid_from <= TS` and not closed at or before it). `TS` is `YYYY-MM-DD` (end of that day, UTC) or an ISO datetime (any offset, normalised to UTC). In dialectic, a fact closed *after* `TS` is listed as current, and the prompt states the reference time. |
| `dialectic` citations | every fact line is labelled `[#id\|rank]` (`user-stated`/`measured`/`inferred`) and the model is told to cite `[#id]`. A deterministic footer follows the answer: `근거: #id (kind·rank·valid_from~valid_to·supersedes #n)` for cited, retrieved facts, `미검증 인용:` for cited ids that were not in the retrieved set, or `인용 없음` when nothing was cited. Same DB + same answer → same footer. |
| `why <id> [--json]` | one fact's provenance on one screen: rank, validity, evidence, `because`, review flag, `source_refs` (as `refs` prints them; a pre-#1264 row says `refs 없음`), what it replaced (`supersedes` chain, ⟵), what replaced it (⟶), and open G3 review siblings (same peer/kind, `review=1`, token overlap ≥ 0.6). Shows nothing beyond the fact text and the stored refs — no transcript text. |
| `why --query <q> [--target T] [--limit N]` | the production recall set rendered in footer form, i.e. what dialectic would have to cite, without calling the model. |

`bench.sh` rows carry `cited_ids=` (sorted, deduplicated `[#id]`s in the answer, or `none`), placed
before `rc=` so existing `rc=… status=… source=…` parsers are unaffected. A usable answer with
`cited_ids=none` is a hallucination suspect.

**Sample noise (#2823).** One synthesis per row let the same Wiki context answer on one run and
refuse on the next, so a node near the threshold could flip PASS/FAIL with no real change. Two
guards, both on by default:

- *One re-synthesis.* A **positive** row whose Wiki synthesis is refused (or empty/invalid) is
  synthesized once more from the same `wiki-agent find` context; either accepted answer counts.
  Negative controls (`source=none`, q44–q48) are never retried — a second draw there could only
  add a fabrication. A timed-out first draw is not retried. Rows that tried it carry
  `resynth=ok|no` before `wiki=`, the ledger gains a 7th column, and the summary adds
  `- wiki re-synthesis (#2823): tried N, recovered M`. `NUNCHI_BENCH_WIKI_RESYNTH=0` turns it off.
- *BORDERLINE band.* An unanswered rate within `NUNCHI_BENCH_BORDERLINE_PCT` points of
  `NUNCHI_BENCH_UNANSWERED_MAX_PCT` (either side; default ±2, so 38–42% at the 40% default) is
  `gate verdict: BORDERLINE — …`, not PASS or FAIL. Width is an integer 0–10 (0 = off; anything
  else falls back to 2). No band when the threshold is 100% or more. Fabrication, contamination
  and backend-degraded verdicts still outrank it. Roll-ups that count PASS/FAIL should count
  BORDERLINE separately.
