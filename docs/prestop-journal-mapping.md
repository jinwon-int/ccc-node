# Pre-stop identity and journal mapping proposal (#1608)

Status: source-only design draft, not an implemented or approved wire contract.
Companions #1609 (in-memory admission), #1610 (durable contract) and #1611
(peer/record policy) are unmerged proposals, not runtime dependencies. The
bounded JSON helper from #1614 supplies bounded decoding and representation-profile
validation only, not versioned schema validation, contextual validation,
authentication or authority. This document
adds no codec, store, controller, endpoint or production caller; no #1608
acceptance criterion is completed. Existing lifecycle defaults stay unchanged.

## Verified baseline and scope

Source baseline: `394de189362ac0f6802b0c8836f34dfade171e3b`.
Open PR inventory checked during drafting: no additional dedicated pre-stop
mapping implementation identified. This is not an exhaustive live controller,
provider, queue or ingress inventory.

| Existing surface | Source at baseline | Consequence |
| --- | --- | --- |
| Handoff request | `bridge/core/restart_handoff.py:228-242` | A 16-hex request ID and mutable v1 receipt identify a request, not a transition lease or authenticated permission. |
| Detached worker | `bridge/core/restart_handoff.py:313-332` | Matching a prepared receipt precedes arming and `systemctl restart`; this path does not acquire the prepared transition lease. |
| Transition ownership | `bridge/prepared_transition.py:72-93` | `active/` is the lease claim; `owner.json` points to a 32-hex run and launcher PID. PID alone does not authenticate its owner. |
| Transition phases | `bridge/prepared_transition.py:21-29,96-133` | v1 validates a fixed phase graph and archives the active lease at terminal phases. New pre-stop phases cannot simply be appended to v1. |
| Receipt persistence | `bridge/core/restart_handoff.py:95-122` | Overwriting publication and ignored directory-sync errors do not supply the proposed immutable publication contract. |
| Transition persistence | `bridge/prepared_transition.py:41-52` | Exclusive creation prevents replacement but writes directly to the final name; complete-before-visible publication is not established. |

Do not silently strengthen the interpretation of existing receipts. In particular,
`armed`, `validated` and `candidate_available` do not prove complete ingress
closure, authenticated consume, or delivery/session continuity.

## Proposed identity mapping for schema review

These are proposed semantic choices for review, not settled cross-PR decisions
or field validation implemented by this draft.

- **Request ID:** preserve the existing handoff request ID only as a correlation
  reference. One request can have multiple explicitly authorized attempts over
  its history; a retry never inherits permission. Do not reuse it as a nonce.
- **Run / lease ID:** use the existing transition run ID as the proposed wire
  `lease_id`, scoped to a particular protected journal root. Compare it to the
  pinned active lease's owner record, not merely to a path supplied by a peer.
  The journal-root binding must come from protected launch configuration; it
  cannot be selected by an incoming record. A run string alone is not a lease.
- **Attempt nonce:** generate a separate fresh 32-byte random nonce (64 lowercase
  hex characters) for one pre-stop attempt under that run. Proposed initial
  scope is exactly one attempt per run. An uncertain or consumed attempt cannot
  be replaced in place, even if a controller exits. Another run requires explicit
  resolution of the existing lease using the retained-pair recovery policy.
- **Controller identity:** bind authenticated process lifetime and launch evidence
  to the lease owner. Keep launcher PID for diagnostics, not as authorization.
  Controller replacement does not resume permission; it requires reconciliation.
- **Work/job IDs:** remain opaque references in their existing subsystem. Do not
  equate a provider task ID, scheduler job, Telegram update, or requesting turn
  with a handoff request, run or attempt. A future ledger adapter must state the
  namespace and acquisition/delivery lifetime for each ingress source.
- **Systemd operation ID:** is a separate manager-scoped identity obtained from
  the effect executor. A transient worker unit name is not the target restart
  job ID. A missing operation ID after an ambiguous submission denies another
  effect until manager/process state is reconciled.
- **Generation:** retain candidate and previous source/runtime pairs as recovery
  inputs, but do not relabel their existing snapshots as protected coherent
  manifest digests. The manifest format and persistent selector remain gates.

The immutable binding is `(protected journal root, run, attempt nonce, boot,
controller identity, serving identity, serving generation, candidate generation)`.
Every peer message and record must be validated against trusted expected values,
not merely against another untrusted record. Per-writer sequence and digest
chains order evidence; they do not authenticate it or prevent whole-store rollback.

## Journal extension boundary

Propose a separately versioned extension of the existing transition journal,
not a second recovery engine. Keep controller and serving evidence in separately
protected writer stores linked to the same run. Exact physical layout, reader
permissions and supported filesystems need review before any writer is added.

Existing v1 readers/writers must not operate on a new-format active attempt.
A later implementation must identify and fence every cooperating entry point
before enabling the new format; merely choosing a new schema string is not a
migration plan. Never edit a retained v1 run, manufacture missing acknowledgements,
or upgrade a mutable receipt into consumed evidence. Legacy records remain legacy.

| Proposed semantic checkpoint | Relationship to existing journal | Required failure behavior |
| --- | --- | --- |
| Intent and pair validation | Reuse claim and retained-pair validation semantics; add attempt bindings and fixed budgets in the new format. | Partial claim retains the lease; dead PID does not authorize reclamation. |
| Closed / draining | New serving-owned evidence after authenticated closure and accounting. No counterpart in v1. | Missing fence or ambiguous delivery forbids permission and effects. |
| Ready | New serving-owned observation of complete fence and empty work/delivery ledger. | Never treat a counter snapshot or health file as readiness authority. |
| Consumed and live reply persisted | New serving consume record plus controller record of a verified live reply. | Disconnect, lost reply or uncertain persistence requires reconciliation, not disk-based permission reconstruction. |
| First effect pending | Extend the existing controller with serialized, revalidated effect execution and operation evidence. | A pending record is intent, not proof the external effect started or did not start. |
| Launch and recovery | Reuse retained-pair launch and one-shot recovery semantics after operation reconciliation. | No competing recovery while a target systemd job may remain active. |
| Terminal and release | Preserve candidate failure versus recovery-success distinction; add required continuity evidence before release. | Evidence failure is not success; do not release merely because an old v1 terminal label is available. |
| Cancel before consumption | New authenticated two-party transition under the same attempt owner. | Persist cancellation before reopening; preserve outstanding work. Partial cancellation needs reconciliation. |

This table does not define an executable phase enum. A follow-up must specify the
full allowed graph, exact role-specific records, cancellation acknowledgements,
and lease release conditions together. Do not add arbitrary payload dictionaries
or a partially permissive codec while these are unresolved.

### Recovery and reopening review cases

The following are required outcomes to test in the eventual graph, not executable
states or a new recovery implementation:

| Observation | Required disposition |
| --- | --- |
| Claim interrupted before `owner.json` or intent publication | Retain exclusion and partial evidence; do not infer an unused lease from a missing record. |
| Drain deadline expires before consumption | Deny an effect. Expiry alone does not reopen admission or release the lease; complete attempt-owned cancellation or reconcile. |
| Serving consume may be durable but its live reply is lost | Treat permission as unavailable to the controller. Do not replay consume or reconstruct a live reply from disk. |
| Controller persisted a reply, then exited before effect submission | Its replacement reconciles; the record does not transfer permission to a new process lifetime. |
| Effect intent exists but systemd submission outcome is unknown | Reconcile the manager-scoped operation and process state before another effect or retained-pair recovery. |
| Cancellation acknowledgement is missing, or consumption raced cancellation | No unilateral reopening or success report. The final graph must define a serialized winner and durable acknowledgement boundary. |
| Candidate fails and the retained previous pair becomes available | Keep the failed update outcome distinct from successful recovery; continuity and release gates still apply. |
| Terminal archival or directory sync fails | Report evidence failure. The existing rename may already have released `active/`; do not claim exclusion remains held or automatically reclaim it. |

The last case is especially important for reuse: `prepared_transition.py:123-133`
already documents release-before-sync uncertainty. A new-format design must
resolve this crash boundary, not claim that the current archival sequence is an
atomic durable release. The scope of attempt-owned reopening (including which
serving generation may reopen) is an explicit phase-graph review gate.

## Effect boundary and budgets

The effect executor must hold the existing cooperating-controller exclusion and
revalidate live peer identity, lease, attempt and remaining effect window before
the first source/artifact mutation or shutdown action. This is a new integration
requirement, not a property of today's `systemctl` call. External lifecycle paths
must cooperate or be explicitly refused in the opt-in mode. Serialization with
noncooperating administrative changes remains an unresolved deployment boundary.

Use one same-boot CLOCK_BOOTTIME overall deadline and bounded drain, persistence,
first-effect, stop, readiness and recovery budgets. Do not restart a deadline on
retry or convert #1609's monotonic floats directly into persisted nanoseconds.
After expiry, no new update effect is authorized; an already-started operation
must be observed and reconciled. Recovery has its own bounded policy within the
overall attempt budget; exhaustion requires explicit intervention, not an
unbounded automatic recovery loop.

The current 30-second restart client timeout and later health timeout are not a
whole-attempt budget. A client timeout does not cancel a systemd job. Effective
unit stop budgets (including the historically observed deployment-specific 70
seconds) need fresh measurement only during separately authorized rollout work.

## Next source-only slice and test gates

Before a semantic codec, resolve these together:

1. Exact identity and protected manifest schemas, journal-root binding and all
   per-kind fields/enums; distinguish nullable values from missing information.
2. Complete ingress/ledger proof format, including fetched-but-undispatched
   Telegram updates, offsets, deferred work, completion/control traffic, outgoing
   multipart retries and the requesting response. Counts alone are insufficient.
3. A maximum-depth-8 representation compatible with #1614's bytes-only parser,
   exact-key validation, int64 bounds and unknown-version rejection. A valid
   parsed object is still untrusted and must not authorize any action.
4. New-format phase graph, lease ownership/release rules and refusal tests for
   old controllers encountering a new-format attempt. No automatic migration.

Then implement the schema validator without filesystem/network/service effects.
Required negative fixtures include cross-run/attempt/boot references, request ID
used as nonce, wrong role/phase, duplicate consume, extended deadlines and legacy
receipt substitution. Context validation must remain distinct from pure syntax
and shape validation; neither substitutes for a real authenticated live peer.

Writer/authentication slices additionally require real-process peer-binding and
exec/delegation tests, protected-root/ACL/filesystem checks, no-replace publication,
and crash/failure injection around every write, sync, reply and effect boundary.
Use hermetic fixtures, not live service control or Telegram. Test ambiguous job
submission and timeouts with an operation that continues after client failure.
No fixtures listed here have been executed by this documentation change.

Independent exact-head review and applicable CI precede merge. Deployment,
provisioning, lifecycle actions and schedule migration need separate approval.
