# Skill promotion: fair collect admission (#1617 / #1647)

How the central publisher's `collect` command admits candidate envelopes
fairly across sources within a run and across runs, and what is guaranteed
versus out of scope.

## Sources

A collect source is either the publisher's own local outbox (label `local`)
or one SSH exporter node (label = node alias, from
`CCC_SKILL_PROMOTION_COLLECT_NODES` / `skill-promotion.collect-nodes`).
The valid remote alias `local` uses source/cursor label `remote:local` to
distinguish it from the publisher outbox; SSH export and ACK still use the
actual alias `local`. Colons are not allowed in node aliases, so this label
cannot collide with another remote. Other labels remain unchanged.
Within a round, `collect_nodes` order is the tie-break and the local outbox
is the canonical first source.

## Within a run: bounded round-robin admission

`_collect_envelopes` gathers from every source up front, interleaving the
results round-robin. Each source gets `floor(64 / source_count)` slots,
with one extra slot for the first `64 % source_count` sources in the rotated
order. This reserves at least one slot for every supported source (up to
32 remotes plus the publisher) without exceeding the global budget, so:

- A full local outbox can never spend the whole budget before an SSH
  exporter is consulted (#1647). The local outbox is bounded to the same
  per-source share as every remote.
- The total admitted in one run never exceeds 64 envelopes, regardless of
  how deep any queue is.
- Per-remote fetches stay bounded by `max_prs` (1..3), matching the exporter
  CLI's `--limit 1..3` contract. The publisher never asks a remote for more
  than the exporter CLI can serve.
- A source whose export or parse fails loses only its own share; the failure
  is reported verbatim in `errors` (`source` + `code`, e.g.
  `remote_export_failed`, `remote_node_mismatch`, envelope validation codes)
  and the remaining sources are still admitted. A malformed or wrong-node
  envelope from one remote never blocks the others.

The publish window is the head of this interleaved list: the loop attempts
rows until `max_prs` successful PR opens, and reports every publish or ACK
failure it passes through instead of hiding it.

## Across runs: the rotation cursor

The publish window is `max_prs` wide, so a fleet with more sources than the
cap needs rotation across runs. Each real collect starts the round-robin
after the source the previous real collect rotated to. Repeated `max_prs=1`
runs therefore cycle through every local and remote source instead of always
beginning with the local outbox (#1647).

The rotation point is persisted in
`<promotion-state-dir>/collect-cursor.json`, an owner-only (0600) JSON
record written atomically with the repository's safe-FS primitives:

```json
{"schema_version":1,"last_source":"<source label>","updated_at":"<UTC>"}
```

- Written only while the promotion lock is held by a real (non-dry) collect.
  `collect --dry-run` neither writes the cursor nor ACKs anything; locked-out
  runs (`status:"locked"`) advance nothing.
- Labels, not positions: reordering, adding, or removing a source can never
  point the rotation at the wrong node. An unknown label falls back to the
  canonical local-first order, so every valid source stays in the cycle and
  no source is permanently starved by fleet changes.
- Missing state is explicit and safe: the run begins at the canonical first
  source, exactly like pre-#1647 collects.
- A present but unreadable or invalid cursor fails closed with a distinct
  code (`collect_cursor_unsafe` for mode/owner/symlink, special-file,
  hardlink, changed-file or bounded-read violations,
  `collect_cursor_invalid` for undecodable content, wrong schema, or a
  malformed label; schema version must be an integer, duplicate keys are
  rejected, and the timestamp must be a nonempty bounded string) instead of silently resetting to the local-first order
  the cursor exists to break.

## What advances the rotation (and what does not)

The cursor tracks admission rotation only. Publish and ACK outcomes never
rewind or wedge it:

- An envelope that fails to publish stays pending (unacked) at its source's
  head — local envelopes remain in `outbox/`, remote envelopes are never
  ACKed — and is retried when the rotation returns to that source.
- An ACK failure (`local_ack_failed`, `remote_ack_failed`) is reported in
  `errors`; the envelope stays pending for the same reason. Errors are
  always surfaced: `ok:false` with per-source `errors` rows.
- A source whose SSH export fails is skipped for that run (its failure
  recorded) and retried next cycle; the rest of the fleet still publishes.
- A single-source fleet has nothing to rotate and writes no cursor file.
- Cursor persistence runs after publication and ACK bookkeeping. A write or
  durability failure preserves all completed `published` rows and adds
  `errors: [{"source":"collect-cursor","code":"collect_cursor_write_failed"}]`,
  making `ok:false`. The cursor may already have changed if directory sync
  failed after rename; the result does not claim that it stayed unchanged.
  An unsafe entry discovered at the final recheck is left in place and
  reported as `collect_cursor_unsafe` (invalid content as `collect_cursor_invalid`).

## Guarantees and non-goals

Guaranteed by this slice: no source can starve another within a run's
admission, and repeated minimal-cap runs reach every source across runs —
including the local outbox and any mix of remote exports — while the global
per-run admission stays bounded at 64 and remote export requests stay within
the exporter CLI limit.

Out of scope (unchanged): `max_prs`, daily caps, the collect schedule, trust
and revision dispatch policy, fleet admission policy, and the #1647
backlog-policy acceptance beyond this fairness slice.
