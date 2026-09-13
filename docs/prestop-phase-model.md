# Pre-stop phase and cancellation graph proposal (#1608)

Status: design documentation only. **Unimplemented and unapproved for runtime
use.** This document proposes an explicit phase graph resolving the five
counterexamples recorded in the 2026-09-13 independent design review of
`docs/prestop-journal-mapping.md` and `docs/prestop-peer-record-policy.md`.
It adds no model implementation, codec, record writer, endpoint, production
caller, deployment, restart or rollout. No #1608 acceptance criterion is
completed. Existing lifecycle defaults remain unchanged.

The state labels below are semantic review handles, not a wire vocabulary or
`kind` enum. Mapping labels to versioned record kinds, exact fields, enum
values and identifier lengths belongs to the peer/record policy and codec
gates and is deliberately not frozen here. Nothing in this graph may be
executed until every gate in the final section is resolved and independently
reviewed.

## Foundations, current status and non-authority

The merged foundations remain source-only groundwork; none of them implements
durable records, peer authentication, effect serialization or recovery:

- #1609 in-process admission (`bridge/core/prestop_admission.py`): one-process
  lock serialization. A `threading.Lock` decides an ordering inside one
  process lifetime; it decides no durable winner and survives no crash.
- #1614 bounded JSON (`bridge/core/prestop_json.py`): syntax helper only.
- #1610 durable contract and #1611 peer/record policy: proposed contracts and
  profiles, not implementations.

Unresolved gates this graph depends on but does not resolve: immutable
attempt binding and protected coherent manifest schemas; race-safe peer and
process-lifetime authentication; complete ingress and outbound delivery
accounting; protected source/artifact/dependency generation provisioning; a
durable no-replace publication writer; v1 compatibility and migration;
serialization with external effects and noncooperating administrators. None
of these is supplied, assumed or bypassed below.

## Roles

| Role | Holds | Must never |
| --- | --- | --- |
| Serving process (current generation) | Admission fence, work/delivery ledger, readiness observation, permission consumption; sole writer of serving-owned records | Treat its own durable disk record as authority to re-issue or reconstruct a live reply; reopen admission on expiry, failure or a lost acceptance; consume after a durable cancellation winner |
| Attempt controller | Requests close and cancellation; verifies live replies; durably records them; submits the one lifecycle effect within the remaining window | Substitute a lost reply from any disk record; start a second effect or competing recovery; extend or restart a deadline |
| Controller successor / reconciler | Observes both stores and manager state; may reconcile | Inherit permission from a predecessor's records or process identity; resolve a both-present conflict by local rule; reclaim a lease automatically |
| Recovery controller (existing retained-pair driver) | Executes validated launch and one-shot recovery with existing semantics | Start while a submitted manager job may still be live; relabel a failed update as recovery success |
| Operator | Resolves latched, conflicted and uncertain states explicitly | — No automated component in this graph acts as operator |

## Attempt spine

One attempt is one lease plus one attempt nonce under one overall deadline
(lengths and representation stay with the record policy). The attempt state
is the pair of serving-owned and controller-owned views; it is
`RECONCILIATION_REQUIRED` whenever the pair is unknown, divergent or
conflicted. Exactly two successful terminal outcomes exist, and every
successful path passes through the states listed here in order:

- **S-CANCEL**: cancellation durably agreed by both sides (serving acceptance
  plus controller acknowledgement receipt), after which serving reopens
  admission as part of the same authorized transition. Outstanding work is
  retained.
- **S-STOP**: the effect was submitted, observed and reconciled, recovery
  reached an existing retained-pair terminal phase with durable evidence and
  continuity gates satisfied, and lease release is durably observable. A
  failed candidate update recovered successfully stays a failed update plus a
  successful recovery; they are never merged into one success.

Anything else is latched failure, an uncertain state below, or
reconciliation. In particular there is no `safe_to_stop` observation, no
`authenticated` flag and no aggregate health counter in this graph; readiness
remains an observation, never permission.

### Spine states

| Label | Views (serving / controller) | Meaning and minimum durable evidence |
| --- | --- | --- |
| `LEASED` | intent / request | Lease claimed and attempt intent recorded; serving admission still open. Existing v1 claim semantics reused; a dead PID authorizes no reclamation. |
| `CLOSE_REQUESTED` | request received? / request persisted | Authenticated close request issued. Until serving's acknowledgement is durably published and its reply verified, no fence is inferred in either direction. |
| `DRAINING` | closed acknowledgement / verified closure reply | Serving owns durable closure of admission; delivery ledger live. Earliest state from which cancellation may be requested. |
| `READY` | readiness record / verified readiness reply | Serving-owned observation of complete ingress fence and empty ledger for this attempt. Still not permission. |
| `CONSUMED_UNREPLIED` | consumed record / nothing verified | Serving durably consumed the permission; the live reply has not been verified by the controller. Serving takes no further action; the controller has no permission from this state. |
| `PERMISSION_RECORDED` | consumed record / durably recorded verified live reply | The only state from which the first effect may be attempted, inside the remaining window. |
| `EFFECT_PENDING` | consumed record / effect intent record | Intent to submit the one effect. An intent record is neither proof that the manager job started nor that it did not. |
| `EFFECT_UNKNOWN` | consumed record / ambiguity note | Submission outcome ambiguous (for example a systemctl client timeout). The manager job may or may not exist and is presumed live until observed otherwise. |
| `EFFECT_RESOLVED` | consumed record / manager-scoped operation identity and outcome | Effect observed and reconciled. Update success versus recovery success remains distinct. |
| `RECOVERY_TERMINAL` | retained-pair run records | Existing v1 phases (`validated` … `recovered`/`recovery_failed`) reused after effect resolution; no parallel recovery engine. |
| `RELEASED` | lease release durably observable | Terminal rename completed **and** the directory synchronization after it completed and was verified. The only successful release. |
| `CANCEL_REQUESTED` | cancel request / persisted request | Cancellation requested from `DRAINING` or `READY` only. Three-leg handshake: request (controller, persisted) → acceptance (serving, persisted) → acknowledgement receipt (controller persists; serving records its arrival). |
| `CANCELLED` | acceptance + ack receipt / persisted acknowledgement | Both sides hold the durable cancellation decision. Serving may reopen admission now, as part of this authorized transition. |
| `CANCEL_REJECTED` | durable rejection | Deadline exhausted, foreign attempt, malformed or post-consumption request. The attempt remains in its prior state; rejection is evidence, not success or failure of the stop. |

### Uncertain states

Each uncertain condition is a distinct state with its own reconciliation
entry. They are terminal for this attempt until an authorized reconciler and,
where required, the operator act. No timeout, retry, process replacement or
subsequent observation moves any of them automatically. An uncertain state is
a specialization of a spine state once a loss or failure is known or
suspected: for example `CONSUMED_UNREPLIED` remains the spine label while the
reply is merely outstanding, and becomes `CONSUME_REPLY_LOST` when loss or
non-delivery is established. The specialization is recorded as evidence; the
underlying disposition does not change retroactively.

| Label | Reached by | What is and is not known | Required disposition; explicitly forbidden |
| --- | --- | --- | --- |
| `CANCEL_REPLY_LOST` | Serving published acceptance; acceptance reply never verified by controller | Serving-side acceptance durable; controller acknowledgement absent | Reconcile; controller must not resend the request as if unanswered and must not treat the acceptance record as its acknowledgement. No reopen, no release. |
| `CANCEL_ACK_PERSIST_FAILED` | Controller received the acceptance but failed to durably record it | Acceptance reply was live; controller-side durability failed | Controller sends no acknowledgement confirmation it cannot first persist; serving without the receipt must not reopen. The disk acceptance record is not an ACK substitute. No reopen, no release. |
| `CANCEL_EXPIRED_PRE_REOPEN` | Overall deadline expired after acceptance, before the reopen-authorizing acknowledgement | Handshake outcome undetermined on the controller side; serving may or may not hold a receipt | Reconcile. Expiry does not cancel, reopen or release by itself; the handshake is not silently restarted. |
| `CONSUME_REPLY_LOST` | Consume record durable; live reply lost (crash, disconnect) | Serving consumed; controller never verified a reply | Serving does not re-issue or replay the reply; controller does not reconstruct permission from disk. Reconciliation only; no effect. |
| `CONSUMED_CANCEL_CONFLICT` | Durable consumed and durable cancel-acceptance records both exist for one attempt | A winner is undefined | Both records are rejected as authority by every role. No recency rule, no role preference, no lock-derived winner. Reconciliation is mandatory; effects and reopen forbidden. |
| `EFFECT_PENDING_ORPHANED` | Controller exited after recording intent, or after submission before recording an outcome | Submission state unknown | Reconcile manager-scoped state before anything else; the job is presumed live. No new effect, no competing recovery, no terminal success claim. |
| `RELEASE_UNCERTAIN` | Terminal rename performed; the following directory synchronization failed, was interrupted, or is unobservable (the rename may have left no trace at all) | Neither "released" nor "still held" can be established | Report evidence failure; preserve all records. No automatic lease reclamation, no cleanup daemon, no reuse of the lease root, no success claim. Resolution reuses the existing retained-pair reconciliation path and requires operator-visible evidence. |

## Allowed transitions

| From → To | Initiating role | Guard that must hold | Durable evidence completing the transition | On failure |
| --- | --- | --- | --- | --- |
| `LEASED` → `CLOSE_REQUESTED` | Controller | Authenticated request bound to lease, attempt, deadline and identities; request persisted before sending | Controller request record | An unverified request confers no fence; a retry is a new authenticated request, never an inheritance |
| `CLOSE_REQUESTED` → `DRAINING` | Serving | Request validates; admission closes atomically with attempt creation | Serving closed-acknowledgement record published before replying | Publication failure keeps the attempt unresolved; controller assumes neither closed nor open; reconcile |
| `DRAINING` → `READY` | Serving | Fence complete; delivery ledger empty for this attempt | Readiness record referencing the closed acknowledgement; fence proof format stays a gate | Stay in `DRAINING`; readiness may not be inferred from counters or health files |
| `DRAINING`/`READY` → `CANCEL_REQUESTED` | Controller | Before any consumption; deadline valid; attempt ownership matches | Persisted cancel request | Post-consumption requests are rejected durably, never honored |
| `CANCEL_REQUESTED` → `CANCELLED` | Serving then controller | Three-leg handshake completes; serving records the acknowledgement receipt before reopening | Acceptance record (serving) + persisted acknowledgement (controller) + receipt of that acknowledgement (serving) | Any loss or failure lands in the matching uncertain state; serving reopens only from the completed handshake |
| `CANCEL_REQUESTED` → `CANCEL_REJECTED` | Serving | Request invalid: expired, foreign, duplicate or post-consumption | Durable rejection record | Rejection is final for that request; it neither cancels nor succeeds |
| `READY` → `CONSUMED_UNREPLIED` | Serving | Recheck peer identity, attempt, deadline, fence and ledger under serving serialization; publish exactly one winner record durably | Consumed record referencing the exact ready record and consume request | Crash before publication leaves no winner; on restart the attempt is unresolved and reconciled, never decided from process memory |
| `CONSUMED_UNREPLIED` → `PERMISSION_RECORDED` | Controller | Live reply received and verified against the attempt | Controller record of the verified reply, persisted before any further action | Lost reply or failed write stays in `CONSUMED_UNREPLIED`; no disk reconstruction, no replay by serving |
| `PERMISSION_RECORDED` → `EFFECT_PENDING` | Controller | Remaining effect window positive; live serving identity revalidated | Effect intent record | Exhausted window forbids the effect; reconcile |
| `EFFECT_PENDING` → `EFFECT_UNKNOWN` | Controller | Submission outcome ambiguous (client timeout or equivalent) | Ambiguity note with request context | This is not a failure of the job and not a success; observe only |
| `EFFECT_PENDING`/`EFFECT_UNKNOWN` → `EFFECT_RESOLVED` | Controller successor or reconciler | Manager-scoped operation observed; job no longer live | Operation identity and outcome record | Unresolved observation keeps `EFFECT_UNKNOWN`; nothing new may start |
| `EFFECT_RESOLVED` → `RECOVERY_TERMINAL` | Recovery controller | No live manager job; outcome recorded | Existing retained-pair journal records | Recovery failure is recorded as non-success and never relabeled |
| `RECOVERY_TERMINAL` → `RELEASED` | Recovery controller | Terminal rename succeeded **and** directory synchronization after it completed and was verified | Release observable and durable | Any failure or doubt is `RELEASE_UNCERTAIN`; no reclaim |
| any nonterminal → reconciliation | Authorized reconciler / operator | State is latched, conflicted or uncertain | Reconciliation decision recorded by the existing lease path | No automatic component may act in the operator's place |

Forbidden in every state: reopening admission after an expired deadline or
latched failure; granting, extending or restarting a deadline; a second
poller or a second effect; journal cleanup or lease reclamation driven by
timeout, PID death or missing records; upgrading a legacy v1 receipt into
phase evidence; deriving authority from any single untrusted disk record.

## Deadline model

| Budget | Set when | Bounds | At expiry |
| --- | --- | --- | --- |
| Overall attempt deadline | Once, by the controller at close; same-boot clock domain per the record policy | Every phase budget below; no phase, retry, successor or reboot extends it | See per-state rows; expiry never cancels, reopens, releases or succeeds by itself |
| Drain budget | At close | `CLOSE_REQUESTED` through `READY` | Consume is foreclosed; only attempt-owned cancellation with the full handshake or reconciliation remains; auto-cancel and auto-reopen forbidden |
| Cancellation handshake budget | At cancel request | The three-leg handshake | An unfinished handshake lands in `CANCEL_EXPIRED_PRE_REOPEN` or its matching uncertain state; it is not silently restarted |
| First-effect window | At `PERMISSION_RECORDED` | The one effect submission | Before start: effect forbidden, reconcile. After start: the running job is not cancelled by expiry; observe and reconcile; no competing recovery; no terminal success claim |
| Effect observation budget | At submission or ambiguity | Observation and reconciliation only | Reconciliation continues as the explicit path; nothing new starts |
| Stop/readiness/recovery/persistence budgets | Per phase inside the overall deadline | Their phase only | Failure is retained evidence; success is never inferred from a budget boundary |

A reboot or unprovable clock continuity makes every budget unverifiable;
the attempt then requires reconciliation and no path may resume on a renewed
timer.

## Crash and failure dispositions

| Failure or crash point | Observable residue | Required disposition | Explicitly forbidden |
| --- | --- | --- | --- |
| During lease claim | Partial claim under existing v1 semantics | Retain exclusion and evidence | Inferring an unused lease from missing records |
| Close request sent, reply unverified | Controller request record only | No fence inferred either way; retry is a new authenticated request | Treating the request as accepted or refused |
| Serving crash after publishing the closed acknowledgement, before reply | Durable closure record, unverified reply | Controller proceeds on nothing; reconcile before any later phase | Acting on an unverified reply in either direction |
| Cancel acceptance published, reply lost | Serving acceptance record | `CANCEL_REPLY_LOST`; reconcile | Substituting the disk acceptance for the lost reply; automatic reopen or release |
| Cancel acceptance received, controller persistence failed | Live acceptance, no controller record | `CANCEL_ACK_PERSIST_FAILED`; serving must not reopen without the receipt | Sending an unbacked acknowledgement; treating acceptance as ACK |
| Deadline expiry before reopen completes | Attempt records as of expiry | `CANCEL_EXPIRED_PRE_REOPEN`; reconcile | Automatic reopen, release or a restarted handshake |
| Crash between consume revalidation and record publication | No winner record | Attempt unresolved after restart; reconcile | Choosing a winner from memory, request order or the in-process lock |
| Consume record durable, live reply lost | Serving consumed record | `CONSUME_REPLY_LOST`; reconcile | Reconstructing the reply or permission from disk; serving replaying the reply |
| Both consumed and cancel-acceptance records durable | Two winner records | `CONSUMED_CANCEL_CONFLICT`; every role rejects both as authority | Recency, role-preference or lock-based winner selection; running any effect |
| Controller persisted the reply, then exited | Controller record under a dead process identity | Successor reconciles; the record transfers no permission | A new process lifetime inheriting effect permission from records alone |
| systemctl client timeout, job state unknown | Ambiguity note | `EFFECT_UNKNOWN`; observe the manager job; presume live | Starting a new or competing recovery; declaring terminal success; treating timeout as cancellation |
| Controller crash after submission, before outcome record | Intent record, outcome unknown | `EFFECT_PENDING_ORPHANED`; reconcile manager state | Assuming the job failed, succeeded or never started |
| Terminal rename done, directory sync failed or unobservable | Renamed entry, unknown durability | `RELEASE_UNCERTAIN`; preserve evidence | Claiming release or continued holding as fact; automatic reclamation or reuse of the lease root |
| Any evidence write fails at any boundary | Partial or unverified evidence | Retain evidence; report failure | Reporting success from or after a persistence failure |

## Forbidden traces

These summarize the graph-level prohibitions; each closes one reviewed
counterexample plus its generalization:

1. **F1 (cancellation acceptance ≠ ACK).** No path may treat a durable
   serving-side acceptance as the controller acknowledgement, reopen
   admission from `CANCEL_REPLY_LOST`, `CANCEL_ACK_PERSIST_FAILED` or
   `CANCEL_EXPIRED_PRE_REOPEN`, or release the lease from any cancellation
   uncertainty.
2. **F2 (durable winner).** No per-process serialization, lock, ordering or
   role preference may stand in for a durable mutual exclusion decision;
   both-present is always `CONSUMED_CANCEL_CONFLICT` and both records are
   rejected as authority; an unprovable winner is reconciliation, never a
   choice.
3. **F3 (disk ≠ live reply; identity ≠ inheritance).** No path may
   reconstruct `PERMISSION_RECORDED` from `CONSUMED_UNREPLIED` disk
   evidence, let a replacement controller identity inherit effect
   permission, or treat `EFFECT_PENDING` as proof a manager job did or did
   not start.
4. **F4 (release uncertainty).** No path may claim `RELEASED` after a failed
   or unobserved post-rename directory synchronization, treat an absent or
   renamed `active/` entry alone as a free lease, or reclaim any lease
   automatically.
5. **F5 (budget vs. submitted job).** No path may start a new effect, a
   competing recovery or a terminal success claim while a submitted job may
   still be live, after overall expiry, or on a systemctl client timeout;
   the only permitted activity is observation and reconciliation.

## Review method

The two successful terminal outcomes (S-CANCEL, S-STOP) are the exhaustive
success claims of this graph. Reviewers should replay each of the five
counterexample traces against both successful paths and against every
transition row, using the uncertain-state and disposition tables above: a
successful path reachable through any forbidden step, or an unmentioned
crash point with no disposition, refutes the graph. For this documentation
change, static checks of document structure, internal consistency and
cross-references suffice; no test in this repository executes this graph,
and none should claim to.

## Remaining gates before any implementation

Everything above is unapproved design. Before any executable model, codec,
writer or caller: immutable binding and coherent manifest schemas; race-safe
peer and process-lifetime authentication; complete ingress and outbound
delivery accounting; protected source/artifact/dependency provisioning and
selection; a durable no-replace publication writer with failure-injection
fixtures; v1 compatibility, fencing of old controllers and migration policy;
and serialization with external effects and administrative changes. Each
requires its own independently reviewed change. Deployment, restart,
schedule and provisioning decisions remain separate explicit approvals under
#1608, and no acceptance box of that issue is closed here.
