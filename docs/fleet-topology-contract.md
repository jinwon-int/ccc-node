# Initial fleet inventory contract v1 (part of #1451 P0)

Status: **documentation, schema, placeholder example and an offline read-only
validator library.** There is still no writer, installer, CLI argument,
enforcement, or runtime wiring, and no consumer has been selected or migrated.
Existing node-local configuration has not been inventoried by this source-only
change. This covers only the initial inventory portion of P0. Full P0 remains
unchecked because relay, team, wiki and keyring-source routing are not modeled.
P1–P4 remain open; this contract does not establish deployment readiness.

| Artifact | Path |
|---|---|
| Contract | `docs/fleet-topology-contract.md` |
| JSON Schema | `schemas/fleet-topology.v1.schema.json` |
| Synthetic example | `docs/examples/fleet-topology.example.json` |
| Offline validator library | `scripts/ccc_fleet_topology.py` |
| Validator fixtures | `scripts/ccc_fleet_topology_test.py` |

## Scope and format

Existing consumers have different hardcoded inventories, including
`scripts/a2a-keyring-drift-check.py`, `scripts/ccc-distill-fleet-matrix.sh`,
`scripts/ccc-doctor-fleet-matrix.sh`, and
`scripts/ccc-security-audit-fleet-matrix.sh`. Different counts alone do not prove
configuration drift: their intended subsets must be preserved explicitly.
The proposed descriptor externalizes node inventory. Role labels describe
inventory; they do not confer trust or authorize remote operations.

The format is UTF-8 JSON with one object root, `version: 1`, and a `nodes` array.
JSON is data, not a shell program. Shell env files were rejected because ordinary
`source`/`.` consumption executes content. YAML would introduce another parser
and typing policy; TOML would introduce another format alongside the repository's
JSON contracts. No parser dependency is added here.

`bridge/core/prestop_json.py` supplies a useful bounded-syntax precedent and
`scripts/agent_cron_schema.py` a schema-validation precedent. Neither is a
topology reader, and neither is imported by `scripts/ccc_fleet_topology.py`:
their *shapes* are followed so the validator stays stdlib-only and free of
bridge packaging. `read_owner_only_bytes` in `bridge/utils/secure_fs.py` was
assessed and rejected for this use — it resolves the whole path through the
kernel, so intermediate symlinks and unsafe parent modes are never inspected,
it defaults ownership to the effective uid, and it opens without `O_NONBLOCK`,
so a FIFO in the descriptor's place would block. Reuse must be re-assessed with
the first real consumer rather than claiming production validation exists now.

Unknown keys are rejected at every object level with `additionalProperties:
false`; platform and role values are closed enums. A consumer implements an
explicit schema revision. Incompatible interpretation changes require a version
bump; adding enum members or fields requires compatibility review, since old
readers reject them. Never silently downgrade or discard unknown data.

## Structural rules

| Field | Required | Rule |
|---|---|---|
| `version` | yes | Numeric value `1` at the schema layer; strict syntax separately requires the integer lexical form. |
| `nodes` | yes | 1–256 node objects, additionally bounded by the whole-file byte limit. |
| `alias` | yes | Lowercase letter followed by 1–31 lowercase letters, digits, or hyphens. SSH alias and inventory join key. |
| `platform` | yes | `linux-systemd` or `android-termux`. |
| `roles` | yes | Unique entries from `bridge-serving`, `a2a-worker`, `a2a-broker`; an explicit empty array is allowed. |
| `repoRoot` | yes | Absolute checkout path; components start with an alphanumeric or underscore, then use alphanumerics, underscore, dot, or hyphen. No trailing/repeated slash or dot-leading component; at most 4096 characters. |
| `endpoint` | no | HTTP or HTTPS URL, at most 2048 characters. A coarse pattern rejects raw whitespace/control characters, `@`, `?` and `#`; a real URL parser and consumer policy supply the remaining checks. |
| `keyRef` | no | `worker:<node>:g<generation>:v<version>` keyring identifier, not key material; generation/version are 1–3 digits. |
| `enabled` | yes | Boolean; absence never implies `true`. |

Every string pattern matches the complete value. The schema uses the absolute
end assertion `(?![\s\S])`: `$` alone can accept a final newline. Aliases cannot
start with `-`, contain shell metacharacters, use `user@host`, or contain path
separators. Eventual callers must still pass values as separate argv elements;
validation is not permission to interpolate them into shell source.

The endpoint is configuration data, not a new transport policy. Both HTTP and
HTTPS, DNS names, IPv4 addresses, bracketed IPv6 addresses, and non-root API paths
can be represented. This preserves existing consumers such as trusted HTTP
loopback tunnels without implying that HTTP is permitted for every destination.
A future consumer must parse the URL and apply its existing transport, trust,
host and path policy. It must not silently change schemes, invent a proxy,
follow an unauthorized destination, or fall back to an old endpoint. Optional
means unavailable when absent, not guessed.

`keyRef` identifies an existing worker public-key entry. A consumer must check
membership, validity, revocation and authorization against a separately trusted
keyring. The descriptor cannot authorize its own keyring or replace that trust
source. Known key-material fields and ordinary key encodings are rejected, but
a schema cannot discover secrets deliberately encoded in a legal alias or path.
Operator review must exclude credentials and private/public key material; schema
acceptance is neither a secret scan nor an authorization decision.

## Semantic rules beyond schema validation

Before any value is consumed, the future validator must also enforce:

1. Aliases are unique across nodes; `keyRef`'s node segment equals its alias.
2. Parse endpoints as absolute HTTP/HTTPS URLs with a nonempty host. Reject
   userinfo, query, fragment, controls, invalid DNS/IP syntax, malformed brackets
   or ports, and encoded authority delimiters. DNS labels are at most 63
   characters and the hostname at most 253; validate IP literals with an IP
   parser. Explicit ports are 1–65535. Validate the path and destination under
   the existing consumer policy, without silently normalizing to a different
   destination. Reserved example endpoints, including `.invalid`, are rejected
   for operational use. Schema acceptance alone is not URL or transport-policy
   validation.
3. The consumer's trusted local identity appears in the inventory. Existing
   identity resolution must be reviewed per consumer; this document creates no
   common identity resolver or precedence bypass.
4. Only enabled nodes in an explicitly selected consumer subset may be used.
   A role is not authorization. Multiple brokers do not imply a first-entry
   winner. Team membership, relay selection, wiki mapping, and keyring
   location/trust are not modeled in this inventory version. Consumers needing
   them must obtain a separately reviewed contract extension or refuse the
   operation; never infer them from array order.

The example's `node-alpha`/`node-beta`/`node-gamma` identities and `.invalid`
endpoint are deliberately synthetic. It is structurally valid documentation,
not a deployable configuration or a runtime fallback. A prepared private
configuration must replace the placeholders and pass all operational rules.

## Proposed path selection and trust boundary

For Linux the proposed default is `/etc/ccc-node/topology.json`, root-owned.
Selection precedence, to be implemented only with a later consumer:

1. An explicit `--topology PATH` on a trusted operator CLI.
2. `CCC_TOPOLOGY_FILE` carrying an absolute path, only in a trusted operator or
   dry-run context.
3. The platform's protected fixed path.

Unattended services, cron jobs, and bridge-injected tools must use a path bound
by their trusted launcher; an untrusted environment cannot choose it. The
highest explicitly selected path is binding even if its file is missing or
invalid. Never fall through to a lower-precedence candidate on failure. There
is no repository copy, `$HOME` search, network fetch, or implicit node list.
These names are proposed interfaces, not flags or variables available today.

Android/Termux cannot generally provision `/etc`. Its trusted launcher must
specify a protected absolute config root and app principal before that consumer
can migrate. Supporting `android-termux` in inventory does not solve that
provisioning gate; do not silently substitute a writable checkout or `$HOME`.

## Required read validation

These requirements are contract obligations. `scripts/ccc_fleet_topology.py`
implements all four layers as an offline library, but **no consumer calls it**,
so nothing here is enforced at runtime yet.

| Layer | Requirement |
|---|---|
| Identity | Open only a regular file, rejecting symlinks, FIFOs/devices and unsafe parents without blocking on special files. Use descriptor-bound traversal and final `O_NOFOLLOW`; path prechecks alone are insufficient. |
| Ownership | File belongs to the configured trusted principal (root for the Linux default), mode `0600` without special bits. Parents belong to platform-approved trusted principals and are not group/other-writable or symlinked. The file cannot declare its own trusted owners. |
| Race safety | Validate metadata on the opened descriptor and bind parent traversal to protected directory descriptors. No re-stat-by-path substitute. |
| Size | Read at most 16,385 bytes and reject over 16,384 before parsing. This byte cap may bind before the structural 256-node cap. |
| Syntax | Strict UTF-8, no BOM, one object root, no trailing data, duplicate keys, floats, NaN/Infinity, or nesting beyond 8. Integer tokens use the existing bounded int64 profile. |
| Structure | Validate against the exact supported schema; reject unknown version/keys/enums, missing fields and invalid types. |
| Semantics | Enforce the separate rules above, including trusted local identity and actual keyring resolution where required. |

JSON Schema treats `1.0` as numerically equal to `1`; the separate syntax layer
must reject that lexical form. Likewise duplicate keys disappear in ordinary
JSON parsing and cannot be detected afterward by a schema validator.

## The offline validator library

`scripts/ccc_fleet_topology.py` is an importable stdlib-only module. It is
deliberately **not** an installed CLI: there is no `__main__` block, no
argument parser, no `setup.sh` manifest entry and no helper package. A
repository-only offline validator needs none of those, and adding them would
imply a node-local install this slice has not reviewed. If a later slice ships
this as an installed CLI, `setup.sh`, `scripts/setup.test.sh` and any real
helper package must be added to the manifest in that same change.

The module reads and validates; it never selects a path, writes, repairs,
publishes, imports a keyring, calls a provider, or opens a connection. Path
selection stays with the trusted launcher described above: the caller passes an
already-open directory descriptor for a root it asserts is trusted, plus a
relative path below it. The module validates every component under that root on
its own descriptor and cannot establish the root's own ancestry — that
obligation is the caller's, and it is why the test fixtures use a private
owner-only root instead of a shared `/tmp` ancestry.

| Entry point | What it establishes |
|---|---|
| `trusted_root(path)` / `read_descriptor(...)` | Metadata layer: descriptor-bound traversal, `O_NOFOLLOW` parents and final open, `O_NONBLOCK` so a FIFO or device cannot block, exact `0600` without special bits, trusted file/parent owners, no group/other-writable parent, 16,384-byte cap, unchanged metadata across the read. |
| `decode_descriptor(payload)` | Syntax layer: bounded strict UTF-8 without BOM, one object root, no trailing data, no duplicate keys, no floats or `NaN`/`Infinity`, bounded int64 integers, depth at most 8, no unpaired surrogates. |
| `validate_structural(payload)` | Syntax + schema structure + content-independent semantics (alias uniqueness, `keyRef` alias binding, real URL/IP/DNS/port parsing). Makes **no** operational claim: `Report.operational_ready` is always false. |
| `validate_operational(payload, context)` | Everything above plus the caller-trusted rules. |
| `require_modeled_capability(name)` | Refuses relay selection, team membership, wiki mapping, keyring location and broker election outright, so they cannot be inferred from roles or array order. |

`DescriptorTrust` configures trusted ownership principals, not a weaker file
format. Its mode must be integer `0600`; its byte cap may only tighten the
fixed limit to an integer in `1..16384`. Invalid values produce
`descriptor_trust_policy_invalid` before descriptor traversal or reading.
The metadata comparison includes link count and change time as well as
modification time, checking the metadata changed by a new hard link or an
edit whose modification time was restored. This is a metadata check, not an
authenticated content snapshot or a lock on an external writer.

`OperationalContext` makes the external obligations explicit rather than
assumed. It carries the consumer's already-trusted local identity, its explicit
alias subset, its own endpoint/transport policy and a resolver backed by a
separately trusted keyring. A selected node with an endpoint and no transport
policy, or with a `keyRef` and no keyring resolver, is **refused** — a `keyRef`
is a reference and can never authorize itself. Reserved example destinations,
including the checked-in `.invalid` placeholder, are accepted structurally and
rejected operationally, which is exactly the separation between the two modes.
Any endpoint whose parse would silently normalize to a different destination is
refused rather than rewritten.
Noncanonical numeric IPv4 forms (including hexadecimal or mixed-base
components) are refused rather than presented as DNS to the policy callback.
IPv4-mapped IPv6 addresses inherit reserved-example classification from their
embedded IPv4 address. These checks do not replace consumer transport policy
or forbid its explicitly permitted canonical loopback tunnels.

Public findings are a stable reason code plus a field location such as
`nodes[2].endpoint`. They never contain an alias, endpoint, repository path or
unknown key name; unknown keys are reported against their parent object only.
Ordinary exceptions from the endpoint-policy and keyring-resolver callbacks
become `operational_endpoint_policy_failed` and
`operational_keyring_resolver_failed` findings, without exception messages or
a partially accepted document. Process-termination exceptions still propagate.

The schema stays the structural source of truth. The module implements only the
draft-2020-12 keyword subset that schema uses and raises
`schema_unsupported_keyword` on anything else, so a schema revision cannot
quietly widen what passes.

## Handling and failure behavior

Never `source`, `.`, `eval`, `bash -c`, expand, or substitute descriptor content
into command source. Pass subprocess data as separate argv elements and pass
query values with APIs such as `jq --arg`. The consumer is read-only: it never
repairs, normalizes, writes or creates the descriptor.

Diagnostics use field names and stable reason codes, not topology content,
aliases, endpoints, or paths in public output. Private operator diagnostics may
identify the selected file when needed to resolve an error.

| Condition in migrated mode | Required behavior |
|---|---|
| Missing, unreadable, or invalid descriptor | Fail the operation, naming the required field/path only in private diagnostics. No lower-precedence or hardcoded fallback. |
| Unknown key/enum/version or missing required field | Reject the entire document; no partial use or synthesized default. |
| Optional endpoint/key reference absent | That capability is unavailable for that node. Do not guess or grant trust. |
| Requested alias absent or disabled | Refuse that node's operation before contact. |
| Duplicate alias, identity mismatch, invalid key reference, ambiguous routing | Fail closed; do not choose a first match. |

Descriptor-level failure is global to the operation. A missing optional
capability may remain node-scoped when the consumer already supports per-node
results. Preserve local-only operations where their established contract does
not depend on remote topology.

## Offline checks and their limits

These commands are inspection aids, not complete admission validation:

```bash
# Basic parse only: jq does NOT reject duplicate keys or floats.
jq -e . docs/examples/fleet-topology.example.json >/dev/null
# With an installed draft-2020-12 validator, structural validation only:
check-jsonschema --schemafile schemas/fleet-topology.v1.schema.json docs/examples/fleet-topology.example.json
# Alternative with compatible ajv-cli:
ajv validate --spec=draft2020 -s schemas/fleet-topology.v1.schema.json -d docs/examples/fleet-topology.example.json
```

No validator dependency or executable is added; the module above is stdlib-only
and is not installed. A path-based `stat`/`namei` inspection cannot establish
race-safe opening, which is why the metadata layer exists at all.

`scripts/ccc_fleet_topology_test.py` supplies the fixtures, collected by
`scripts/validate-harness.sh`:

```bash
python3 scripts/ccc_fleet_topology_test.py
```

They cover the example, unknown keys, wrong types/enums, unsafe aliases/paths,
URL credentials/query/control characters, duplicate roles and trailing newlines
in every patterned field, plus duplicate aliases, key-reference mismatch,
URL/IP/port parsing, consumer transport policy, absent local identity, reserved
example endpoints, syntax-only failures, and the metadata cases —
symlinked file and parent, FIFO/socket/device without blocking, unsafe parent
ownership and modes, non-`0600` and special-bit files, and the byte cap. Do not
describe a structural pass as a complete configuration or deployment check.

A consumer migration still needs more than this library: a reviewed transport
policy, an actual keyring authorization source, and the routing decisions this
inventory version does not model. Missing any of those means incomplete
validation and blocks migration, whatever the validator reports.

## Per-consumer migration (P1+, not performed)

| Consumer | Inventory use | Remaining gate |
|---|---|---|
| `scripts/ccc-node-status.py` | Requested peer membership and checkout path | Preserve local status; refuse remote operation in migrated mode without valid topology. |
| Doctor/security-audit/distill fleet matrices | Explicit intended subset of enabled aliases | Preserve each tool's subset semantics; an explicit `--node-list` cannot bypass migrated validation. |
| `scripts/a2a-keyring-drift-check.py` | Worker inventory | Team/broker selection and trusted keyring location need additional contract decisions; roles alone are insufficient. |
| Identity resolvers | Membership check only | Review each resolver's existing trust/precedence; do not silently replace identity resolution. |

Each migration is a separately reviewed opt-in change. Before opt-in, legacy
behavior stays unchanged. Migrated code must have no hardcoded fallback; any
retained legacy branch must be explicit and separately tested. Delete it only
after a reviewed rollout and rollback checkpoint. P2 bridge wiring and P3
narrative cleanup are not included here. P4 repository-wide scan enforcement
is a ratchet, not a zero rule: `scripts/canon-node-name-scan.sh --repo-wide`
compares per-file node-name hit counts against the committed
`scripts/canon-node-name-baseline.txt` (validate-harness static phase). New
files and higher counts fail; a lower count fails until the baseline is
regenerated with `--update-baseline`, so every migration above locks its
progress in. The baseline is generated, never hand-edited.

## Node-local preparation and rollback plan

This is a plan, not an executed provisioning operation. First inventory existing
consumers and confirm the protected root/principal for the platform. Author
actual values in an exclusively created owner-only staging file in that root,
not a predictable shared `/tmp` path. Run all four validation layers, preserve
any existing file in an owner-only backup, and verify that backup before
replacement. Do not install the checked-in synthetic example verbatim.

Publication needs same-filesystem atomic rename, file and parent-directory
sync, and verification of the opened final file. Ordinary `install source target`
does not promise atomic replacement. P0 supplies no writer implementing this.

Define a consumer-specific rollback checkpoint before migration. Stop new
migrated operations, explicitly restore reviewed legacy mode if retained, then
restore a validated backup or archive the new descriptor. Do not permanently
delete operator configuration or claim success if restoration conflicts. A
missing descriptor intentionally fails closed in migrated mode. Keep the
consumer's opt-in reversible and preserve independently valid local operations.

No live configuration, provider request, deployment, restart, or runtime state
change was performed by this source change. The document defines obligations
for future consumers and does not claim they are currently enforced.
