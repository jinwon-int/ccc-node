# Pre-stop peer and record policy proposal (#1608)

Status: review proposal, documentation only; not an approved deployment policy.
Companion to PR #1610 (durable integration contract) and PR #1609 (in-memory
admission foundation). Neither is assumed merged. This proposal supplies no
endpoint, codec, writer, authentication implementation or lifecycle caller.
No acceptance box in #1608 is closed. Existing defaults remain unchanged.

## Threat boundary and decisions proposed for review

The initial implementation should target Linux on one host and one boot. Deny
cross-host, cross-namespace and reboot-spanning permission use. Trust the kernel,
protected launcher and expected principals; do not claim protection against a
compromised root or a compromised authorized serving process. Same-UID process
isolation is not supplied by private file modes. A valid JSON record, file owner,
hash, PID or caller-supplied identity alone never authenticates permission.

Use a pathname Unix-domain stream socket, not TCP or an abstract socket. Resolve
its location from protected launch configuration, never HOME, repository data,
request payload or an untrusted environment variable. Socket permissions are an
initial access check, not peer authentication. Reserve deployment-specific paths
and numeric identities for separately reviewed provisioning; do not create them
as a side effect of parsing or connecting.

The controller and serving process each verify the other. Require kernel peer
credentials and a pinned process-lifetime reference (Linux pidfd), plus boot ID,
process start ticks, expected UID/GID, exact unit invocation and generation from
trusted launch evidence. Refuse if the supported kernel/runtime cannot establish
a race-safe binding between socket credentials and the pinned process. Reading
`/proc/<pid>` after receiving a PID does not by itself resolve PID reuse. A pidfd
pins process lifetime, not executable identity across exec: the launch policy
must forbid unobserved exec/descriptor delegation and invalidate authority on
identity changes. Recheck at consume and at the first effect boundary.

The controller identity must match the existing transition lease holder, not
merely any process in an allowed unit or UID. A fresh challenge bound to the
connection and attempt provides liveness, not authorization. Fail closed on
missing unit metadata, namespace mismatch, process exit or inconsistent evidence.
The implementation must identify and test the precise kernel/process-binding
mechanism before an endpoint is enabled; no best-effort credential fallback.

## Storage principals and provenance

Keep controller-owned and serving-owned records in separately provisioned,
private directories, with each writer denied write access to the other's store
where distinct principals permit this. For a same-root deployment this is an
accidental-write boundary only, not cryptographic separation. Do not introduce
an alternate service account or change permissions as part of the source slice.

Use a protected persistent storage root; the socket may use a protected runtime
root. Neither a reboot-cleared socket directory nor a user-writable checkout is
a durable journal. Bind records to the existing lease/journal attempt; do not
create an independent recovery ledger, automatic cleanup daemon or reclamation
policy. Exact mapping to the retained-pair journal is a prerequisite to writer
implementation, not something inferred from this proposal's field names.

A serving-owned consumed record is historical evidence, not a bearer capability.
The controller records the authenticated live reply durably before any effect.
After disconnect, crash, lost reply or uncertain persistence, enter explicit
reconciliation. Never reconstruct effect permission from a copied disk record.
Hash chaining detects accidental substitution only when the expected head is
trusted; it does not authenticate writers or stop rollback of an entire store.

## Proposed version-1 encoding profile

Use bounded UTF-8 JSON objects with exact keys per record kind. Reject duplicate
keys, unknown keys/versions/kinds, non-object roots, trailing data, invalid UTF-8,
BOM, floats, NaN/Infinity and booleans where integers are required. Read at most
16 KiB plus one overflow-detection byte; bound nesting before recursive parsing.
No coercion, defaults or automatic migration of security-relevant fields.
Canonical writer output uses sorted keys, compact separators and one final LF;
reader acceptance is semantic, not dependent on whitespace. Hash exact published
bytes when referring to a previous record. No secrets or message content.

Proposed common fields (all required):

| Field | Proposed constraint |
| --- | --- |
| `version` | Integer 1 only |
| `kind` | Exact per-role phase enum; never arbitrary command text |
| `attempt_nonce` | 32 cryptographically random bytes, lowercase hex |
| `lease_id` | Exact existing lease identity; representation must be mapped before codec implementation |
| `sequence` | Positive integer up to 2^63-1; per-writer ordering only |
| `previous_sha256` | Lowercase 64-hex previous-record digest; null only at the writer's first record |
| `boot_id` | Canonical lowercase Linux boot UUID |
| `controller`, `serving` | Exact identity objects: UID, GID, PID, start ticks, unit invocation ID; no assertion substitutes for peer validation |
| `serving_generation`, `candidate_generation` | Digests of protected coherent manifests, including source, artifacts and dependencies; Git SHA alone is insufficient |
| `issued_boottime_ns`, `deadline_boottime_ns` | Integer nanoseconds in Linux CLOCK_BOOTTIME; 0 <= issued < deadline <= 2^63-1 |

Use CLOCK_BOOTTIME to include suspension in elapsed budgets; reject a different
boot, future issue time or exhausted deadline. Do not translate PR #1609's
in-memory clock values into these fields without an explicit reviewed adapter.
The controller establishes the overall deadline once; no phase, retry or restart
may extend it. Phase budgets and the first-effect window are separately bounded
by that deadline. Detect clock inconsistencies as reconciliation-required.

Role/phase-specific payload schemas must enumerate exact fields for intent,
closed, ready, consumed, cancellation and lifecycle outcome. Ready includes a
versioned ingress-fence proof and empty work/delivery ledger evidence. Consumed
references the exact ready record and authenticated consume request. Counts alone
are not a fence proof. Defining these payloads depends on the complete ingress
inventory; do not ship a permissive arbitrary payload object in the interim.

## Protected publication protocol to implement and test later

1. Validate provisioned ancestors, ownership, modes and ACL policy. Open and pin
   directory descriptors without following symlinks. Verify namespace/mount
   assumptions; reject network/unsupported filesystems. Descriptor-relative I/O
   prevents path traversal but does not make writable ancestors safe.
2. Under the existing serialized attempt owner, create a private temporary regular
   file using exclusive, no-follow creation. Check owner/mode and link count;
   write all bounded bytes with short-write handling, then synchronize the file.
3. Publish an immutable sequence record using an atomic **no-replace** operation
   on the same filesystem, then synchronize its directory before replying.
   Ordinary overwriting rename is insufficient. A conflicting final name is a
   reconciliation condition, even if its bytes match; never blindly retry it.
4. Readers use no-follow, nonblocking open and descriptor validation before a
   bounded read, rejecting symlinks, FIFOs, devices, extra links, wrong modes,
   oversize/partial records and changed metadata. Validate expected attempt,
   role, phase, sequence and trusted predecessor, not just syntactic validity.
5. Any error after publication may mean the record exists without known durable
   completion. Retain evidence and deny effects; do not delete the final record,
   roll back consumption, reopen admission or infer success from a subsequent
   read. Reconciliation is explicit and reuses the existing retained-pair path.

No publication sequence makes an external `systemctl` call atomic with a record.
Effect serialization, external service changes and persistent generation
selection remain blocking integration decisions, not guarantees of this store.

## Required evidence and remaining gates

Before codec work: resolve exact lease mapping, identity/manifest formats,
per-kind fields, nesting limits and enum values. Before writer work: resolve
protected provisioning, supported filesystem semantics and journal ownership.
Before authentication work: select and independently test race-safe peer binding.

Required fixtures (not executed by this documentation change): separate real
processes, wrong identity/unit/boot, PID reuse and exec/descriptor delegation;
symlink/ancestor/ACL attacks, FIFO nonblocking refusal and competing writers;
short writes and failure/kill before and after file sync, no-replace publication,
directory sync and reply; copied/rolled-back records and lost consumed replies;
suspend-expired deadlines and every unsupported-platform refusal. Kernel failures
must not be reduced to success by mocks. Use temporary user-owned fixture roots,
not production directories, and never invoke live Telegram or service control.

Merge requires independent exact-head review and green applicable CI. Endpoint
integration still requires complete ingress/delivery accounting, authenticated
serving-side consume, effect serialization and retained-pair reconciliation.
Rollout, restart, schedule changes and provisioning require separate approval.
