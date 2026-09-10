# Pre-stop bounded JSON syntax helper

Related: #1608. This is isolated, source-only groundwork, not production wiring.
`bridge/core/prestop_json.py` has no production callers. It grants no permission
and does not implement the proposed peer-record policy in PR #1611.

## API and profile

`decode_object(data: bytes) -> dict[str, Any]` accepts **exact** built-in bytes
(not subclasses, strings, bytearrays or memoryviews). It raises
`PrestopJSONError`, a `ValueError`, for rejected input.

- Maximum encoded size: 16,384 bytes, inclusive, including whitespace.
- Strict UTF-8, no leading BOM; escaped valid surrogate pairs are accepted,
  unpaired surrogates in any decoded key or value are rejected.
- At most eight simultaneous object/array levels, counting the root object.
  A string-aware scan enforces this before the recursive standard JSON decoder.
- Exactly one object root, with JSON whitespace allowed before and after it.
- Duplicate decoded keys are rejected at every depth, including escaped aliases.
  Unicode normalization is not performed; canonically equivalent but distinct
  strings are distinct keys. This is not a canonical serialization format.
- Integers are restricted to signed 64-bit values. Fractions, exponents, NaN and
  infinities are rejected, even if numerically integral. JSON negative zero is
  accepted as integer zero.
- Strings, arrays, objects, booleans and null remain valid nested values.
- Error messages omit record content; decoder exceptions are suppressed in the
  default traceback. This is not secret erasure: debuggers, exception contexts
  and locals may retain input. Do not log locals or supplied records.

## Explicit boundaries

The caller must cap reads **before** allocating input; rejecting a large supplied
bytes object does not bound upstream buffering. These limits bound decoder input
and nesting, not measured wall-clock time or total process memory. No performance
or hostile-process isolation guarantee is made.

This module does not validate field allowlists, required fields, version tags,
per-kind enums, exact boolean-versus-integer types, identities, paths, timestamps,
budgets, lease/journal correspondence or coherent manifests. Unknown fields are
accepted. It does not authenticate peers, check replay, open files, protect
storage, publish records, take locks or mutate lifecycle state. Output is mutable
untrusted data and must never be used as authorization by itself.

Full schema and authenticated durable admission remain blocked on the existing
lease/journal mapping, peer-binding mechanism, storage/provisioning policy and
external-effect serialization. No #1608 acceptance criterion is completed by
this syntax helper. No installation, restart, schedule change or update reenable
is part of this change.

## Verification scope

`bridge/tests/test_prestop_json.py` covers supported scalars/containers, byte and
depth boundaries, escaped strings and surrogate pairs, nested/escaped duplicate
keys, malformed syntax, numeric bounds, invalid encoding, exact input types,
content-free errors and rejection before recursive decoding. The tests explicitly
preserve the distinction between syntactic acceptance and schema authority.

Run from `bridge/` with the repository test environment:

```sh
python -m pytest tests/test_prestop_json.py -q
```

These are unit tests only. No peer/process authentication, filesystem durability,
crash injection, production ingress/egress or lifecycle integration is tested.
