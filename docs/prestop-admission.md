# Pre-stop admission foundation (#1608)

**Source-only, in-process state machine. No production callers. Not a durable
handshake, restart controller, authentication mechanism, or deployment approval.**
Existing lifecycle and updater behavior is unchanged.

## Contract

`bridge/core/prestop_admission.py` serializes transitions with one lock. A serving
process creates one `PrestopAdmission` using its process-start identity (not PID
alone), coherent source/artifact/dependency generation, and a monotonic clock.
These identifiers are caller assertions, not independently verified facts.

- `admit()` returns a unique work token only while open. Admission must precede
  enqueue/dispatch; hold the token through provider work, all output parts and
  final outbound delivery. Keep it during retryable delivery failures.
- `close(timeout=...)` atomically closes admission and creates an attempt-bound
  `StopEvidence`. Concurrent closes cannot replace its owner. Timeouts must be
  positive and finite. They cover this drain only, not the whole update.
- `finish(token, delivered=True)` removes exactly one known token. Unknown or
  duplicate completion cannot decrement another work item. Terminal delivery
  failure (`False`) latches the gate closed, including when no attempt exists.
- `ready(evidence)` is an observation, not permission. `commit(evidence)` checks
  identity, state, deadline and empty work again, consuming the transition once.
  No further admission or cancellation is possible after commit.
- `cancel(evidence)` reopens only a still-valid, unexpired draining attempt and
  invalidates its evidence. Outstanding tokens survive and block a later drain.
  Foreign/stale evidence cannot cancel someone else's attempt.
- An observed expired deadline, backward/nonfinite clock or clock exception
  latches the gate closed. Failed states cannot be canceled or reset by this API.
  Completion of remaining tokens is still accepted but does not clear failure.
  Reconciliation is intentionally not implemented; replacing the object to
  bypass a failure is not a supported recovery procedure.

The clock is a trusted, nonblocking, non-reentrant callback invoked under the
lock. Evidence is immutable but is **not a secret or signed capability**: copied
matching fields work in the same object. Attempt equality prevents accidental
cross-attempt reuse, not malicious callers. Thread serialization does not provide
cross-process synchronization. Crashes erase the entire state. No timers run:
deadlines are checked by readiness, commit and cancellation operations.

## Required integration before any production use

1. Inventory and wire every ingress: queued messages, cron/background/provider
   work, retries, outbound delivery and the response requesting the transition.
   Define rejected ingress defer/retry and Telegram offset handling explicitly;
   do not acknowledge then silently discard work. Never start a second poller.
2. Implement authenticated durable IPC with serving-process acknowledgement
   bound to attempt, process-start identity, generation and deadline **before
   source/artifact mutation or any stop signal**. A returned Python object or
   successful `commit()` is not that acknowledgement. Persist crash states and
   ownership, reject replay/stale evidence, and define attempt-owned reopen and
   terminal-delivery reconciliation. Failure to prove safety must refuse stop.
3. Reuse the prepared-runtime/retained-pair recovery design, not a parallel
   recovery engine. Protect the complete root execution chain (checkout, index,
   loaders and ancestors), temporary state, backups, dependencies and coherent
   generation selection. Git `safe.directory` alone establishes none of this.
   Every enabled lifecycle controller must cooperate with the same lease/lock.
4. Budget stop, start, readiness, recovery and evidence persistence separately.
   A systemctl client timeout does not cancel a continuing systemd job; never
   launch competing recovery on that assumption. This module's drain timeout
   does not prove any existing updater whole-attempt bound sufficient.
5. Require separately approved rollout plus verification of source/runtime
   identity, fresh health, response/session continuity, single-poller ownership
   and recovery integrity. No deployment, signal, scheduler change or privilege
   expansion is part of this slice.

## Validation

`bridge/tests/test_prestop_admission.py` exercises work/delivery lifetime,
one-use commit, stale/foreign attempts, cancellation with pending tokens,
latched terminal failure, deadlines/clock failures and concurrent admission/commit.
It uses a fake clock and threads, with no live service/provider/Telegram calls.
These tests do not establish durable crash recovery, authentication, actual
systemd behavior or complete accounting of production work.
