# Experimental fixed-conversation Grok runtime (#1632)

`GrokRuntime` now implements the provider-neutral session/event seam against
the [qualified transport](GROK-BOT-TRANSPORT.md). This is an isolated component;
`CCC_AGENT_PROVIDER=grok` is **not yet registered or enabled**. Do not claim
Telegram delivery, production rollout, approval integration or issue completion
from the component tests.

## Explicit identity and initialization

A `GrokBinding` binds the configured SSH destination, existing Bot UUID,
owner-conversation label and local working-directory label. Its deterministic
session ID identifies that exact binding. The directory label scopes the
bridge attachment; it does not change the remote Bot's working directory.

`GrokJournal.create()` is an explicit management operation creating a new
private directory and initial binding. It rejects an existing directory,
including partially initialized state. Runtime open never invokes it.
`start_or_resume()` requires the exact persisted session ID and directory.
Missing/corrupt state, arbitrary session IDs, `/new`, model, effort, sandbox,
approval-policy/reviewer and memory options are denied. Model discovery is empty
because the Bot's model is managed externally.

The caller must enforce the actual authenticated owner/audience mapping and
one configured journal for this Bot. Journal locking does not coordinate
independently configured roots, other computers or another Grok app client.
These constraints must be enforced in the future provider factories, not
represented as optional UI advice.

## State and transitions

The private directory contains an immutable lock name and numbered canonical
JSON revisions. Initial revision 0 binds schema 1, host version and identity.
Each revision contains the SHA-256 of the previous raw revision plus the
current operation. All revisions remain available; there is no pruning,
reset, stale-claim requeue, deletion of unknown state or recovery import.

An operation records its nonce, exact prompt/digest and pre-send baseline,
then proceeds through these states:

| State | Meaning | Reopen behavior |
| --- | --- | --- |
| attempted | Durable before the single send call; may not have reached host | Query acceptance for this nonce; never resend |
| accepted | Matching account/Bot/nonce/digest/echo persisted | Wait for and validate the attributable reply |
| complete | Bound reply persisted before any output | Read the cached result |

Immutable intent fields and acceptance cannot change during transitions.
The full contiguous revision chain and every operation are checked on load.
Nonce reuse across operations is rejected. Incomplete/unknown files, missing
revisions, invalid canonical JSON, schema/identity drift, symlinks, hardlinks,
unsafe permissions, directories and FIFOs deny. Reads use nonblocking,
no-follow descriptors; state is 0600 and the directory exactly 0700. All state
reads and writes occur under the process claim. Current lock/directory identity
is checked to detect name replacement. New revision content and directory
publication are fsynced before the caller proceeds. Partial pending files are
retained and deny subsequent opening.

The limit is 97 revisions (initial + 32 three-stage operations), 256 KiB per
revision, approximately 24.25 MiB maximum record payload. The runtime reserves
three revision slots before a new attempt. It does not silently increase these
limits. Prompt, reply, baseline and wire limits from the transport still apply;
serialized size can constrain pathological escape-heavy input more tightly.

The journal is private plaintext, not encrypted at rest. Its hash chain detects
accidental corruption and missing/interior changes, **not malicious replacement
or rollback to a complete valid older prefix**. No external monotonic witness
exists. A copied/rolled-back journal must not be used to claim remote
exactly-once execution or independent active replicas.

## Events, retry and interruption

The session serializes local turns. A separate process/nonshared session trying
the same journal receives a busy error instead of interleaving a send. The
nonblocking claim is held for the bounded operation; there are no blocking
network calls on the event-loop thread. This is a local operation lease, not a
host-side CAS.

Only a committed bound result yields buffered `TextDeltaEvent` and
`MessageCompletedEvent`, then one `ResultEvent` and terminal `CompletionEvent`.
Output never precedes journal completion. These are final-buffer events, not
live token streaming. An error retires the session and is nonretryable; explicit
reopen can reconcile the same retained operation. A different input cannot
displace an uncertain pending operation.

The session seam has no stable incoming-message/attempt ID or delivery ack.
Therefore an input identical to the latest complete prompt conservatively
returns its cached reply, including after restart, without another send.
Intentional immediate repetition cannot request a new execution in this slice.
A different input after completion begins a new operation. This does **not**
certify Telegram delivery or deduplicate arbitrary delayed platform redeliveries;
the bridge's inbound and outbound delivery boundaries still need qualification.

Each turn has a 180-second operation deadline; polling waits two seconds while
the configured Bot is busy, but an approval-only or foreign/invalid state denies.
The SSH transport retains its own 20-second exchange and two-second cleanup
bounds. Unknown send outcomes and no-longer-visible transcript ranges are
retained, not retried under a fresh nonce.

`interrupt()` cancels the local wait, retires the handle and returns
`grok_interrupted_outcome_unknown`. It does not call the host-global interrupt
API, undo tools or certify remote cancellation. Interrupt/close prevents later
buffered output. Existing Bot tool policy remains external; no fabricated
approval event or decision is sent. Rejecting an approval/tool transcript after
the fact does not prevent a side effect. Full user-facing behavior must clearly
mark this axis unsupported/degraded before activation.

## Qualification and next integration boundary

Generated fake transport tests run the actual runtime and real disk journal.
They include the shared normalized event-stream contract, accepted-send response
loss, cached-result reopen, changed pending input, interleaved foreign reply,
unsupported identity/options, concurrent sessions/processes, partial disk writes,
corruption and local cancellation before/after result commit.

Actual subprocess SIGKILL tests cover death immediately before sending, after
the generated host accepted, and after the result committed. Reopen makes zero
new send calls for those operations. This is hermetic host data, not a live
Telegram crash test. Earlier live transport evidence and any live runtime probe
are retained separately with exact source hashes and scope.

Next, register the provider only when config, readiness, capability matrix,
factories, fixed-owner routing, session reset/resume and rollout/rollback all
agree. The current generic fallback to other-provider readiness must never
handle a Grok configuration. An isolated allowlisted Telegram DM, with a
dedicated token and one poller, remains required before closing #1632.
