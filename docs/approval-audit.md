# Approval snapshot and audit contract

`approve-each` requests use one provider-neutral owner contract for Claude and
Codex. This contract does not widen the selected execution profile and does not
change `auto-approve`, `auto-review`, or `disabled` behavior.

## Owner display

Before Telegram renders an approval, the bridge builds a bounded snapshot with:

- provider, normalized action, and target shape;
- a target-only command/path/permission preview;
- an optional working-directory hint and fixed risk hints;
- a request fingerprint and a fingerprint of the exact final Telegram text.

The renderer removes ANSI and bidirectional/control characters, omits raw
environment/header/file-body fields, applies the shared credential redactor,
and only then truncates UTF-8 output. Telegram receives the final text with
`parse_mode=None`. Raw provider descriptions and arguments never enter the
audit ledger.

The in-memory request binding is keyed HMAC-SHA-256, which prevents the ledger
from becoming a dictionary oracle for low-entropy commands. The already-safe
display text uses ordinary SHA-256 so an owner can later verify an exact copied
prompt. A changed request fingerprint invalidates the old one-shot token and
requires a new prompt even when the provider reuses its request id.

## Owner-only ledger

Pending `approve-each` requests append to:

```text
BOT_DATA_DIR/approval-audit/approval-audit.jsonl
```

The directory and files must be exactly `0700` and `0600`. Symlinks, hardlinks,
wrong owners, unsafe modes, and unsafe ancestors are rejected. Writes are
bounded and locked; malformed old rows are retained but ignored during dedup
and aggregation. Storage failures are body-free and fail-open for
observability, while the existing generation, expiry, fingerprint, and
one-shot-token checks remain fail-closed for execution.

Each approval has at most one `asked` and one terminal `answered` record. A
terminal record contains only opaque session/turn/request/actor references,
provider/action/shape, request and display fingerprints, fixed redaction and
display-field labels, decision/reason, timestamps, and latency. Callback/text
races and late replays cannot append a second terminal decision.

## Body-free metrics

Inspect aggregate action, decision, reason, and latency counts without printing
request references or fingerprints:

```bash
python -m telegram_bot.core.approval_audit \
  --directory "$BOT_DATA_DIR/approval-audit" --json
```

The report is evidence for later policy review only. It never creates an
allowlist or changes approval policy automatically.
