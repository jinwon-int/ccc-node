# Nunchi channel collection

Check the serving provider, bridge memory mode, BOT_DATA_DIR, and configured
audience root for each frontend. A fresh ingest tick proves execution; it does
not prove coverage of the active frontend's journal.

For a routed Matrix journal, run the existing mirror with explicit paths:

```sh
BOT_DATA_DIR=/absolute/matrix-data \
CCC_NUNCHI_AUDIENCE_ROOT=/absolute/configured-audiences \
NUNCHI_JOURNAL_CHANNEL=matrix \
python3 "$HOME/.claude/hooks/nunchi/journal-feed.py"
```

The opaque route in each completed job selects the destination. Missing,
non-private, symlinked, or inconsistent destinations fail closed; no route or
scope directory is inferred. The configured root can be shared by frontends
only when the bridge already uses that root and namespaces their scopes.
Unrouted legacy journals still belong to the legacy feed. Do not route them
to `shared` or a private audience by guessing from a username or session id.

An explicit channel gets its own status file and input/destination-bound
receipt ledger. Old receipts remain available when configuration changes.
`held` reports exhausted retries; a tick with no new work is not evidence
that a held failure recovered. The advisory Jev reviewer accepts the same
`CCC_NUNCHI_AUDIENCE_ROOT` override.

Danso supports `install-nunchi.sh --apply --danso --audience-scoped ROOT`.
It mirrors completed extractions with no additional model call. The bridge
must enable extraction with a finite provider budget. Its native state tree,
like Claude's global transcript tree, is not audience-separated; the installer
therefore omits a global MemPalace refresh for either scoped provider.

Before enabling `--judge-apply`, preserve the existing install flags and
audience root. The judge backs up the store and only clears eligible review
flags. Reasonless decisions, duplicate merge proposals, conflicting or failed
judgments remain pending. A dry-run intentionally makes no progress in the
queue; do not use its repeated successful verdicts as application evidence.

Jev remains advisory and never changes source memory. Its closed technical
vocabulary admits known Korean words with grammatical particles while keeping
the original wording. Identifiers are redacted and unknown or denied terms
stay local. No cursor is automatically rewound when the vocabulary changes.

Synthesis health expires after 24 hours by default
(`NUNCHI_BACKEND_HEALTH_MAX_AGE_SEC`, bounded to 10 minutes–7 days). The state
becomes `stale`; previous outcomes remain in the status history. A fresh
connectivity probe is needed before treating an old fallback as a current
provider outage.
