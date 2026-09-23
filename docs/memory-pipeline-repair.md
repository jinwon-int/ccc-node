# Collection receipts and advisory classification review

Codex and Piri feeds now acknowledge a source fingerprint only after successful
JSON extraction and ingestion. A growing session is reconsidered. Failures get
three attempts with ten-minute backoff; changing the source enables a new
attempt. Legacy path-only seen files remain untouched. Their last seven days
are reconsidered on upgrade; use `NUNCHI_FEED_REPLAY_DAYS` for deliberate older
backfill. A receipt means the ingest gate accepted the payload, not that every
candidate survived its existing duplicate/mutability/reason gates.

`journal-feed.py` mirrors completed bridge extractions into the journal's exact
private/shared audience. Missing routes are reported and never inferred. It
makes no model calls, disables automatic supersession for historical replay,
and keeps receipts only after ingest and snapshot succeed. Failed jobs back off
so one malformed file cannot indefinitely block later jobs. Legacy unrouted
journals remain with the existing legacy ingestion path.

Run the scoped mirror from an enabled node's normal runtime HOME:

```sh
python3 ~/.claude/hooks/nunchi/journal-feed.py
```

`jev-review.py` is a separate opt-in cron consumer, after collection. Set
`NUNCHI_JEV_REVIEW=1`; the key is read from
`~/.secrets/typesafe-api-key` (`NUNCHI_JEV_KEY_FILE` overrides). First invocation
records each DB's current maximum ID without sending historical facts. New DBs
or replaced DB inodes also establish a baseline. Only new, open facts are
considered. No keys or raw response bodies are logged.

The external boundary is deliberately narrow: technical-topic screening,
sensitive-topic rejection, identifier redaction, then a hand-maintained closed
English/Korean operational vocabulary. Unknown words, names and credential-like
strings remain local. This sacrifices coverage: many ordinary sentences will be
skipped. Vocabulary expansion requires review; never learn the allowlist from
raw user memories. Sanitization is not a claim of semantic completeness.

The pinned Jev model is `jev-1.13.0`, using the frozen nine-kind plus `uncertain`
choice rubric. Redirects are rejected. Requests time out after ten seconds.
Maximum five attempts/run and100/day per runtime HOME across all memory scopes.
Attempts are reserved durably before network I/O and never automatically
replayed, including crashes/timeouts. Source memory is opened read-only. A
corrupt DB does not block review of healthy scopes.

Results live only in `~/.nunchi/jev-review.db` (`reviews` table). Original kind,
proposed choice, confidence, status and local fact reference are available there;
no kind/review/authority updates are applied to source memory. Missing key,
API failure or disagreement cannot prevent memory collection. This is advisory
review, not truth verification or automatic Wiki publication.

Example separate schedule (existing collector stays installed):

```cron
3,13,23,33,43,53 * * * * python3 $HOME/.claude/hooks/nunchi/journal-feed.py >> $HOME/.nunchi/journal-feed.log 2>&1
5,15,25,35,45,55 * * * * NUNCHI_JEV_REVIEW=1 python3 $HOME/.claude/hooks/nunchi/jev-review.py >> $HOME/.nunchi/jev-review.log 2>&1
```

Deploy all sibling helpers together and preserve node-local backups. Rollback:
remove these two schedules, restore feed scripts from backup, keep the receipt
and advisory databases as evidence. No source-memory rollback is needed for Jev.
