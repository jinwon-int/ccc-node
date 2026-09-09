# Durable pre-stop handoff contract (#1608)

Status: proposed integration contract, documentation only. No production wiring,
receipt implementation, authentication, restart or rollout is supplied here.
PR #1609's in-process admission foundation remains independently reviewable;
its Python evidence is not durable authority. No #1608 acceptance box is closed
by this document. Existing lifecycle defaults remain unchanged.

## Source-pinned inventory

Inspected at main `45203a398853bac81350413406dc43749c0c067e`.
These are starting seams, not a complete enabled-controller or ingress inventory.
Paths below are repository-relative; line numbers refer to that commit.

| Seam | Observed source | Required integration proof |
| --- | --- | --- |
| Telegram polling | `bridge/core/bot_lifecycle.py:777-787`, `:1119` | Initial polling may drop pending updates; reconnect also starts polling. Fence both paths before declaring ingress closed. |
| Message age filtering | `bridge/core/bot_access.py:46-61` | Existing stale-message rejection must not silently discard accepted/deferred work after a long drain. Define retention and age-policy interaction explicitly. |
| Accepted task queue | `bridge/core/task_queue.py:54-77`, `:95` | Acquire a work token before accepting/enqueueing, not only on provider dispatch. Task completion alone is not delivery acknowledgement. |
| Provider admission | `bridge/core/project_chat.py:1163-1174` | `begin_drain()` closes new provider turns, not every external ingress. Account for existing and waiting work separately. |
| Outbound streaming | `bridge/core/streaming.py:223`, `:265`, `:584` | Keep tokens through final edits, overflow chunks and retries; aggregate final delivery results, including non-streaming sends. |
| Scheduled/background work | `bridge/core/bot_lifecycle.py:1730` | Distill extraction has its own scheduling loop. Inventory continuation, push, external-wait and other background loops too; prove each participates or is safely deferred. |
| Busy observation and signal drain | `bridge/core/bot_lifecycle.py:2077-2135` | Snapshot uses max of counters, not an admission ledger. Signal drain can time out or be forced; neither authorizes a pre-stop mutation. |
| Detached restart | `bridge/core/restart_handoff.py:322-330` | Receipt arming precedes a restart command with a 30-second client timeout; introduce a durable permission boundary before this command or mutation. |
| Existing recovery | `docs/prepared-runtime-launch.md`, receipts and recovery section | Reuse retained-pair transition evidence, cooperating-controller lease and stable-inode poller lock. Legacy commands do not currently share that lease. |

External cron, timers, updater processes and effective unit settings require a
separate live inventory before rollout. This source inspection does not assert
which ones are enabled. Control commands and permission callbacks required to
finish already admitted work must remain available during a drain; they must
not provide a route to admit unrelated new work.

## Roles, identity and trust

- Serving process owns admission closure and final work/delivery accounting.
- One controller owns the existing transition lease and the candidate/retained
  generation pair. Lease ownership does not let it forge serving acknowledgement.
- A local authenticated channel must verify the expected peer, not just accept
  PID, UID or generation strings in a message. On Linux, peer credentials are
  only part of the check: bind boot identity, process start identity and unit
  membership to the expected serving generation. Root execution additionally
  requires protected source, index, loaders, dependencies and ancestors.
- Version every record. Bind every request/reply to a fresh unpredictable attempt
  nonce, lease identity, boot/process-start identity, serving generation,
  candidate generation and phase. Reject unknown versions, wrong phase,
  duplicate consumption, stale generations and conflicting records.
- Deadline evidence needs a specified same-boot monotonic clock domain and an
  overall attempt deadline. A serialized `time.monotonic()` value is not portable
  evidence across reboots. Reboot or uncertain clock continuity requires
  reconciliation, never a renewed timeout on replay.

Exact wire encoding, protected channel location, peer policy, durable record
schema and managed generation selector are unresolved implementation decisions.
No endpoint may be enabled until these are specified and independently tested.

## Proposed transition and durability order

Names here describe required semantics, not a new parallel recovery engine.
Map them to the existing transition journal/lease before writing a controller.

| Phase | Required action before advancing | Crash or ambiguous result |
| --- | --- | --- |
| Intent | Claim existing lease; validate trusted coherent candidate and retained pair; persist attempt identity and bounded budgets. Preparation must not mutate the serving generation. | Keep lease/evidence; do not infer cancellation from dead controller PID. |
| Close requested | Authenticate request; atomically fence all relevant admission and ingress paths; persist serving-owned closed acknowledgement. | Missing acknowledgement prohibits mutation and signal. Uncertain closure requires reconciliation. |
| Draining | Retain every admitted token through final outbound success, including the requesting response; allow required completion/control traffic only. | Terminal or ambiguous delivery failure prevents permission. Do not replay provider work automatically. |
| Ready | Persist serving-owned readiness for this attempt only after complete ingress fence and empty delivery ledger. | Readiness is an observation, not reusable stop permission. |
| Consumed | Controller requests one-use permission; serving process rechecks identity, deadline, fence and ledger, then durably consumes permission before replying. Controller durably records verified reply before any mutation/signal. | Lost reply or failed controller persistence prohibits proceeding; consumed state must not be reopened by timeout. |
| Lifecycle pending | Use existing retained-pair controller; record operation identity and outcome. Maintain admission fence and lease. | A systemctl client timeout does not cancel the job. Reconcile its job/process state before any recovery. |
| Terminal | Verify selected serving generation, readiness and delivery/session continuity; record candidate or one-shot recovery outcome using existing semantics. | Recovery success is still candidate-update failure. Evidence-write failure is not success. |

Consumption is not an indefinite capability: before the first lifecycle effect,
the controller must revalidate attempt ownership, serving identity and the
remaining bounded effect window. Expiry or identity uncertainty after consumption
prohibits starting that effect and requires reconciliation without reopening.
If an effect already started, deadline exhaustion does not cancel it or authorize
a competing operation. Specify how the effect boundary is serialized with other
controllers and external service changes; a check followed by an unguarded
`systemctl` call is not an atomic permission boundary.

Durable publication requires complete writes, file synchronization, atomic
publication and parent-directory synchronization in a protected directory.
Reject symlinks, wrong ownership/modes, record substitution and partial records;
validate directory ancestors and concurrent writers, not just final file mode.
A file appearing on disk is neither authenticated evidence nor proof that its
publication completed. Failures at any persistence boundary retain evidence
and deny further lifecycle effects.

Cancellation is an authenticated two-party transition, allowed only before
permission consumption, with matching ownership and a valid deadline. Persist
cancellation before reopening; outstanding work survives. A crash between
persistence and reopen is reconciled explicitly. Neither an expired deadline,
failed delivery nor replacing the in-memory object authorizes reopening.
No automatic lease reclamation, interrupted-attempt resume or journal cleanup.

## Telegram acceptance and delivery policy to prove

Do not equate stopping handlers with fencing `getUpdates`: the library can fetch
and advance offsets while updates still wait in its queue. Before opt-in use,
choose and test one end-to-end ingress strategy that proves:

1. Already fetched/accepted updates are accounted for before readiness.
2. Updates arriving after the fence are retained for later handling, or durably
   deferred with defined ownership; none are acknowledged then discarded.
3. Planned restart does not apply the current initial-backlog-drop behavior to
   deferred updates. The existing message-age filter must not silently drop
   accepted/deferred work either. Reconnect/watchdog paths cannot undo the fence.
4. No second poller starts until the existing stable-inode token lock permits it.
5. Retry/defer notices are themselves outbound work if relied on for acceptance.

Do not claim exactly-once Telegram delivery: a lost network reply can leave a
send's remote outcome unknown. Treat ambiguous final send/edit results as
reconciliation-required, not success or permission to blindly resend. Retain
only bounded operational metadata (update/work IDs, phase and message IDs where
known), not message bodies, credentials or model outputs in handshake records.

## Implementation and test gates

The following are required tests, **not tests executed by this documentation PR**:

- Admission before/after close across queue, provider, cron/background and every
  delivery path; multipart/retry failure; requesting response still outstanding.
- Actual local peer process with wrong UID/start identity/boot/generation;
  stale/copied/replayed records and competing controller/commit/cancel requests.
- Failure injection before/after each write, fsync, publish, reply and consume;
  kill either process at each boundary and verify no unsafe reopen or stop.
- Real polling adapter fixtures for fetched-but-undispatched updates, reconnect,
  initial backlog handling and arrival while fenced; no live Telegram calls.
- Poller overlap, symlink/ancestor substitution, untrusted loader/index and
  partial coherent-generation installation refusal.
- Retained-pair fixtures with distinct dependencies; systemd-job stub continuing
  after client timeout; no competing recovery; recovery failure stays non-success.
- Explicit bounded drain, stop, readiness, recovery and persistence budgets,
  including outer watchdog exhaustion at every phase.

Suggested next source-only implementation PR: versioned durable record codec
and protected publication reader/writer with failure-injection and real-process
fixtures, after peer/record policy is resolved. Do not wire the restart command
until authenticated serving-side consumption and complete ingress accounting
are independently reviewed. Rollout and schedule migration remain separate
explicit approvals under #1608.
