"""Offline read-only validator for the fleet inventory descriptor v1 (#1451).

This module is an importable library, not an executable entry point: there is
deliberately no ``__main__`` block, no argument parser and no installer wiring.
It implements the four read-validation layers the contract in
``docs/fleet-topology-contract.md`` requires — descriptor metadata, strict
syntax, schema structure and semantics — and nothing else. It selects no
descriptor path, migrates no consumer, and performs no SSH, keyring, provider
or network access.

What this module does NOT establish
-----------------------------------
Passing :func:`validate_structural` means the bytes are a well-formed v1
inventory document. It is *not* a deployment check, a transport decision, a
secret scan or an authorization decision. :func:`validate_operational` adds the
semantic rules that need caller-supplied trust, and *refuses* rather than
guesses when those external obligations are absent: a trusted local identity,
an explicit consumer subset, the consumer's own endpoint/transport policy, and
a separately trusted keyring resolver. ``keyRef`` is a reference only and can
never authorize itself. Relay selection, team membership, wiki mapping,
keyring-source routing and broker election remain unmodeled in v1;
:func:`require_modeled_capability` refuses them explicitly instead of letting a
caller infer them from array order.

Reuse assessment
----------------
``bridge/core/prestop_json.py`` is the bounded-syntax precedent and
``scripts/agent_cron_schema.py`` the schema-derived-validation precedent; both
shapes are followed here. Neither is imported: this validator must stay
stdlib-only and free of bridge packaging. ``read_owner_only_bytes`` in
``bridge/utils/secure_fs.py`` was assessed and is not sufficient here — it
resolves the whole path through the kernel (so intermediate symlinks and
unsafe parent modes are never inspected), it defaults ownership to the
effective uid, and it opens without ``O_NONBLOCK``, so a FIFO in the
descriptor's place would block. The contract requires descriptor-bound parent
traversal, which is implemented locally below.

Diagnostics are stable reason codes plus a field location such as
``nodes[2].endpoint``. They never contain aliases, endpoints, repository
paths, unknown key names or any other descriptor content.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
from functools import lru_cache
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "fleet-topology.v1.schema.json"
)

MAX_DESCRIPTOR_BYTES = 16384
MAX_JSON_DEPTH = 8
MIN_INT64 = -(2**63)
MAX_INT64 = 2**63 - 1
REQUIRED_FILE_MODE = 0o600
UNSAFE_PARENT_MODE_MASK = 0o022
MAX_ENDPOINT_CHARS = 2048
MAX_HOSTNAME_CHARS = 253
MAX_DNS_LABEL_CHARS = 63

MODE_STRUCTURAL = "structural"
MODE_OPERATIONAL = "operational"

#: Questions this inventory version actually models. Everything else is
#: refused by :func:`require_modeled_capability` rather than inferred.
MODELED_CAPABILITIES = frozenset(
    {
        "node-inventory",
        "node-membership",
        "node-enablement",
        "node-platform",
        "node-repo-root",
    }
)

#: Reserved / documentation namespaces that must never be contacted. Structural
#: mode accepts them (the checked-in example uses one); operational mode does not.
_RESERVED_TLDS = frozenset({"invalid", "example", "test", "localhost"})
_RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
_RESERVED_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)

# Path components accepted by descriptor traversal. The class excludes "",
# "." and ".." by construction, so no dot-leading or traversal component can
# be expressed; it matches the contract's repoRoot component rule.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_IPV4_SHAPE_RE = re.compile(r"^[0-9.]+$")
_NUMERIC_IP_COMPONENT_RE = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")
_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}$")


@dataclass(frozen=True, slots=True)
class Finding:
    """A stable public reason code bound to a field location.

    ``location`` names fields and array indices only (``nodes[2].endpoint``);
    it never carries a value read out of the descriptor.
    """

    code: str
    location: str = ""

    def __str__(self) -> str:
        return f"{self.location}: {self.code}" if self.location else self.code


class TopologyError(ValueError):
    """Fail-closed validation error carrying a content-free :class:`Finding`."""

    def __init__(self, finding: Finding) -> None:
        super().__init__(str(finding))
        self.finding = finding


@dataclass(frozen=True, slots=True)
class DescriptorTrust:
    """Ownership and size expectations for the descriptor and its parents.

    ``file_uids`` are the principals allowed to own the descriptor (root for
    the Linux default). ``parent_uids`` defaults to ``file_uids`` and applies
    to every directory traversed *below* the caller-trusted root. The
    descriptor cannot declare its own trusted owners, so these come from the
    caller, never from the file.

    ``max_bytes`` may tighten the fixed v1 limit, never exceed it.
    ``required_mode`` must remain exactly 0600. Invalid policies fail before
    descriptor traversal; these fields cannot relax the format's guarantees.
    """

    file_uids: frozenset[int]
    parent_uids: frozenset[int] | None = None
    max_bytes: int = MAX_DESCRIPTOR_BYTES
    required_mode: int = REQUIRED_FILE_MODE

    def parents(self) -> frozenset[int]:
        return self.file_uids if self.parent_uids is None else self.parent_uids


@dataclass(frozen=True, slots=True)
class EndpointFacts:
    """Parsed endpoint facts handed to a consumer's own transport policy."""

    scheme: str
    host: str
    host_kind: str  # "dns" | "ipv4" | "ipv6"
    port: int | None
    path: str
    reserved_example: bool


@dataclass(frozen=True, slots=True)
class OperationalContext:
    """Caller-supplied trust that this descriptor cannot supply for itself.

    ``local_alias`` is the consumer's already-trusted local identity — this
    module resolves no identity and bypasses no precedence. ``selected_aliases``
    is the consumer's explicit subset; a role is not authorization and an empty
    subset is refused rather than widened. ``endpoint_policy`` is the
    consumer's existing transport/trust policy, and ``keyring_resolver`` must
    be backed by a separately trusted keyring. When a selected node needs one
    of the callbacks and it is absent, validation fails closed.
    """

    local_alias: str
    selected_aliases: frozenset[str]
    endpoint_policy: Callable[[EndpointFacts], bool] | None = None
    keyring_resolver: Callable[[str], bool] | None = None


@dataclass(frozen=True, slots=True)
class Report:
    """Result of one validation pass.

    ``operational_ready`` is never true for a structural pass, so a structural
    result can never be reported as a configuration or deployment check.
    """

    mode: str
    findings: tuple[Finding, ...]
    notices: tuple[Finding, ...] = ()
    document: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def operational_ready(self) -> bool:
        return self.mode == MODE_OPERATIONAL and not self.findings

    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


def require_modeled_capability(capability: str) -> None:
    """Refuse any routing question inventory v1 does not model.

    Relay selection, team membership, wiki mapping, keyring location and
    broker election are outside this contract version. Callers needing them
    must obtain a separately reviewed contract extension; they must not be
    inferred from roles or array order.
    """

    if capability not in MODELED_CAPABILITIES:
        raise TopologyError(Finding("capability_not_modeled", capability))


# --- layer 1: descriptor metadata -------------------------------------------


@contextmanager
def trusted_root(path: str | os.PathLike[str]) -> Iterator[int]:
    """Open a directory descriptor the CALLER asserts is trusted.

    This module cannot establish that trust: the caller's launcher chooses a
    protected root (``/`` for the Linux default, a platform-approved protected
    root on Termux) and is responsible for its ancestry. The directory is
    opened ``O_NOFOLLOW`` so the final component itself cannot be a symlink,
    and every component *below* it is validated by :func:`read_descriptor`.
    """

    _require_descriptor_platform()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _cloexec()
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise TopologyError(_open_finding(error, "descriptor.root")) from None
    try:
        yield fd
    finally:
        os.close(fd)


def read_descriptor(root_fd: int, relative_path: str, trust: DescriptorTrust) -> bytes:
    """Read the descriptor under ``root_fd`` with descriptor-bound traversal.

    Every intermediate component is opened ``O_DIRECTORY | O_NOFOLLOW``
    relative to the previous descriptor and validated on that descriptor, so a
    swapped parent cannot be re-stat'ed by path afterwards. The final component
    is opened ``O_NOFOLLOW | O_NONBLOCK`` — a FIFO or device in its place is
    rejected by ``fstat`` instead of blocking the open. At most
    ``trust.max_bytes`` bytes are accepted, and the descriptor metadata must be
    unchanged across the read.
    """

    _check_descriptor_trust(trust)
    _require_descriptor_platform()
    components = _relative_components(relative_path)
    parent_uids = trust.parents()
    open_fds: list[int] = []
    try:
        current = root_fd
        for component in components[:-1]:
            current = _open_directory(component, current, open_fds)
            _check_parent(os.fstat(current), parent_uids)
        return _read_regular_file(components[-1], current, trust)
    finally:
        for fd in reversed(open_fds):
            os.close(fd)


def _check_descriptor_trust(trust: DescriptorTrust) -> None:
    if (
        type(trust.required_mode) is not int
        or trust.required_mode != REQUIRED_FILE_MODE
        or type(trust.max_bytes) is not int
        or not 1 <= trust.max_bytes <= MAX_DESCRIPTOR_BYTES
    ):
        raise TopologyError(Finding("descriptor_trust_policy_invalid", "descriptor"))


def _require_descriptor_platform() -> None:
    # Without O_NOFOLLOW/O_DIRECTORY the traversal cannot be made race-safe,
    # so refuse rather than silently downgrade to a path precheck.
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise TopologyError(Finding("descriptor_platform_unsupported", "descriptor"))


def _cloexec() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def _relative_components(relative_path: str) -> list[str]:
    if not relative_path or relative_path.startswith("/"):
        raise TopologyError(Finding("descriptor_path_not_relative", "descriptor.path"))
    components = relative_path.split("/")
    for component in components:
        if _COMPONENT_RE.match(component) is None:
            raise TopologyError(Finding("descriptor_path_component", "descriptor.path"))
    return components


def _open_finding(error: OSError, location: str) -> Finding:
    if error.errno == errno.ELOOP:
        return Finding("descriptor_symlink", location)
    if error.errno == errno.ENOENT:
        return Finding("descriptor_missing", location)
    if error.errno == errno.ENOTDIR:
        return Finding("descriptor_parent_not_directory", location)
    if error.errno == errno.ENXIO:
        # A socket or a write-only-side special file: not a regular file.
        return Finding("descriptor_not_regular", location)
    if error.errno in {errno.EACCES, errno.EPERM}:
        return Finding("descriptor_unreadable", location)
    return Finding("descriptor_open_failed", location)


def _open_directory(component: str, parent_fd: int, open_fds: list[int]) -> int:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | _cloexec()
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(component, flags, dir_fd=parent_fd)
    except OSError as error:
        # The open already failed closed; the lstat below only sharpens the
        # reason code, because O_DIRECTORY | O_NOFOLLOW reports a symlinked
        # parent as ENOTDIR rather than ELOOP on Linux.
        finding = _open_finding(error, "descriptor.parent")
        if finding.code in {"descriptor_symlink", "descriptor_parent_not_directory"}:
            finding = Finding("descriptor_parent_not_directory", "descriptor.parent")
            try:
                if stat.S_ISLNK(
                    os.stat(component, dir_fd=parent_fd, follow_symlinks=False).st_mode
                ):
                    finding = Finding("descriptor_parent_symlink", "descriptor.parent")
            except OSError:
                pass
        raise TopologyError(finding) from None
    open_fds.append(fd)
    return fd


def _check_parent(metadata: os.stat_result, parent_uids: frozenset[int]) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise TopologyError(Finding("descriptor_parent_not_directory", "descriptor.parent"))
    if metadata.st_uid not in parent_uids:
        raise TopologyError(Finding("descriptor_parent_untrusted_owner", "descriptor.parent"))
    # The sticky bit does not rescue a group/other-writable parent: a shared
    # world-writable directory such as /tmp is never a trusted ancestry.
    if stat.S_IMODE(metadata.st_mode) & UNSAFE_PARENT_MODE_MASK:
        raise TopologyError(Finding("descriptor_parent_writable", "descriptor.parent"))


def _stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _read_regular_file(component: str, parent_fd: int, trust: DescriptorTrust) -> bytes:
    flags = (
        os.O_RDONLY | os.O_NOFOLLOW | _cloexec() | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(component, flags, dir_fd=parent_fd)
    except OSError as error:
        raise TopologyError(_open_finding(error, "descriptor.file")) from None
    try:
        before = os.fstat(fd)
        _check_file_metadata(before, trust)
        if before.st_size > trust.max_bytes:
            raise TopologyError(Finding("descriptor_too_large", "descriptor.file"))
        payload = _read_bounded(fd, trust.max_bytes)
        if _stat_signature(os.fstat(fd)) != _stat_signature(before):
            raise TopologyError(Finding("descriptor_changed", "descriptor.file"))
        return payload
    finally:
        os.close(fd)


def _check_file_metadata(metadata: os.stat_result, trust: DescriptorTrust) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise TopologyError(Finding("descriptor_not_regular", "descriptor.file"))
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise TopologyError(Finding("descriptor_special_bits", "descriptor.file"))
    if mode != trust.required_mode:
        raise TopologyError(Finding("descriptor_mode", "descriptor.file"))
    if metadata.st_uid not in trust.file_uids:
        raise TopologyError(Finding("descriptor_untrusted_owner", "descriptor.file"))
    if metadata.st_nlink != 1:
        raise TopologyError(Finding("descriptor_multiple_links", "descriptor.file"))


def _read_bounded(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        try:
            chunk = os.read(fd, min(65536, remaining))
        except OSError:
            raise TopologyError(Finding("descriptor_unreadable", "descriptor.file")) from None
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise TopologyError(Finding("descriptor_too_large", "descriptor.file"))
    return payload


# --- layer 2: strict syntax --------------------------------------------------


def _syntax_error(code: str) -> TopologyError:
    return TopologyError(Finding(code, "descriptor.syntax"))


def _check_depth(text: str) -> None:
    # Scanned before the recursive stdlib decoder (the prestop_json precedent).
    # Braces and escaped quotes inside strings are not nesting.
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _syntax_error("syntax_depth_exceeded")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                raise _syntax_error("syntax_invalid")


def _bounded_integer(text: str) -> int:
    # Bound the conversion independently of the interpreter's digit limit.
    if len(text.lstrip("-")) > 19:
        raise _syntax_error("syntax_integer_out_of_range")
    value = int(text)
    if not MIN_INT64 <= value <= MAX_INT64:
        raise _syntax_error("syntax_integer_out_of_range")
    return value


def _reject_float(_text: str) -> Any:
    # Rejects every floating-point lexical form, including the `1.0` that JSON
    # Schema would accept as numerically equal to the `version` const.
    raise _syntax_error("syntax_float_forbidden")


def _reject_constant(_text: str) -> Any:
    raise _syntax_error("syntax_constant_forbidden")


def _unique_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Duplicate keys vanish in ordinary parsing and cannot be detected
            # by a later schema pass, so they are rejected here.
            raise _syntax_error("syntax_duplicate_key")
        result[key] = value
    return result


def decode_descriptor(payload: bytes) -> dict[str, Any]:
    """Decode bounded, strict-UTF-8 JSON with one object root.

    Rejects oversize input, invalid UTF-8, a BOM, unpaired surrogates,
    duplicate keys, floats, ``NaN``/``Infinity``, integers outside the bounded
    int64 profile, nesting beyond eight levels, trailing data and a non-object
    root. Field names, versions and every authority check belong to the
    structural and semantic layers.
    """

    if type(payload) is not bytes:
        raise _syntax_error("syntax_not_bytes")
    if len(payload) > MAX_DESCRIPTOR_BYTES:
        raise _syntax_error("syntax_byte_limit_exceeded")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _syntax_error("syntax_invalid_utf8") from None
    if text.startswith("\ufeff"):
        raise _syntax_error("syntax_bom_forbidden")
    _check_depth(text)
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_pairs,
            parse_int=_bounded_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError:
        raise _syntax_error("syntax_invalid") from None
    if not isinstance(document, dict):
        raise _syntax_error("syntax_root_not_object")
    _reject_surrogates(document)
    return document


def _reject_surrogates(document: Any) -> None:
    pending: list[Any] = [document]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in item):
            raise _syntax_error("syntax_surrogate_forbidden")


# --- layer 3: schema structure ----------------------------------------------

# Only the draft-2020-12 keywords the checked-in schema actually uses are
# implemented. An unimplemented keyword is a hard error rather than a silent
# skip, so a schema revision cannot quietly weaken validation here.
_SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "const",
        "enum",
        "pattern",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "required",
        "additionalProperties",
        "properties",
        "items",
    }
)


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Load the repository-owned schema; it is the structural source of truth."""

    value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TopologyError(Finding("schema_not_object", "schema"))
    return value


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _json_type_matches(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return value is True or value is False
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in {int, float}
    if expected == "string":
        return type(value) is str
    if expected == "array":
        return type(value) is list
    if expected == "object":
        return type(value) is dict
    raise TopologyError(Finding("schema_unsupported_type", expected))


def _same_json_value(left: object, right: object) -> bool:
    # Type-aware equality so True never matches 1 and 1 never matches 1.0.
    if (left is True or left is False) != (right is True or right is False):
        return False
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    return left == right


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _location(path: str) -> str:
    return path or "descriptor"


def _check_keywords(schema: Mapping[str, Any]) -> None:
    for keyword in schema:
        if keyword not in _SUPPORTED_KEYWORDS:
            raise TopologyError(Finding("schema_unsupported_keyword", keyword))


def _validate_schema_node(
    value: object, schema: Mapping[str, Any], path: str
) -> list[Finding]:
    _check_keywords(schema)
    location = _location(path)
    declared = schema.get("type")
    if declared is not None:
        expected = [declared] if isinstance(declared, str) else list(declared)
        if not any(_json_type_matches(value, item) for item in expected):
            return [Finding("structure_type", location)]
    findings: list[Finding] = []
    if "const" in schema and not _same_json_value(value, schema["const"]):
        findings.append(Finding("structure_const", location))
    if "enum" in schema and not any(
        _same_json_value(value, option) for option in schema["enum"]
    ):
        findings.append(Finding("structure_enum", location))
    if type(value) is str:
        findings.extend(_validate_string(value, schema, location))
    if type(value) is list:
        findings.extend(_validate_array(value, schema, path, location))
    if type(value) is dict:
        findings.extend(_validate_object(value, schema, path, location))
    return findings


def _validate_string(
    value: str, schema: Mapping[str, Any], location: str
) -> list[Finding]:
    findings: list[Finding] = []
    if len(value) < schema.get("minLength", 0):
        findings.append(Finding("structure_min_length", location))
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        findings.append(Finding("structure_max_length", location))
    pattern = schema.get("pattern")
    # Length is checked first so an oversized value never reaches the regex.
    if pattern is not None and not findings and _compiled(pattern).search(value) is None:
        findings.append(Finding("structure_pattern", location))
    return findings


def _validate_array(
    value: list[Any], schema: Mapping[str, Any], path: str, location: str
) -> list[Finding]:
    findings: list[Finding] = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        findings.append(Finding("structure_min_items", location))
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        findings.append(Finding("structure_max_items", location))
        return findings
    if schema.get("uniqueItems") is True:
        for index, item in enumerate(value):
            if any(_same_json_value(item, other) for other in value[:index]):
                findings.append(Finding("structure_duplicate_items", location))
                break
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            findings.extend(_validate_schema_node(item, item_schema, f"{path}[{index}]"))
    return findings


def _validate_object(
    value: Mapping[str, Any], schema: Mapping[str, Any], path: str, location: str
) -> list[Finding]:
    findings: list[Finding] = []
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in value:
            findings.append(Finding("structure_required", _join(path, key)))
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise TopologyError(Finding("schema_unsupported_keyword", "additionalProperties"))
    if schema.get("additionalProperties") is False:
        # The unknown key itself is untrusted input and is never echoed; the
        # parent location plus a stable code is the whole public diagnostic.
        if any(key not in properties for key in value):
            findings.append(Finding("structure_unknown_key", location))
    for key, item in value.items():
        child = properties.get(key)
        if isinstance(child, dict):
            findings.extend(_validate_schema_node(item, child, _join(path, key)))
    return findings


def validate_structure(document: Mapping[str, Any]) -> list[Finding]:
    """Validate a decoded document against the checked-in v1 schema."""

    return _validate_schema_node(document, load_schema(), "")


# --- endpoint parsing --------------------------------------------------------


def _endpoint_error(code: str) -> TopologyError:
    return TopologyError(Finding(code, "endpoint"))


def _parse_port(text: str) -> int:
    # A leading zero or a "+"/whitespace form would normalize to a different
    # literal, so only the canonical decimal form is accepted.
    if _PORT_RE.match(text) is None:
        raise _endpoint_error("endpoint_port")
    port = int(text)
    if not 1 <= port <= 65535:
        raise _endpoint_error("endpoint_port")
    return port


def _parse_ipv6_host(netloc: str) -> tuple[str, str, int | None]:
    end = netloc.find("]")
    if end < 0:
        raise _endpoint_error("endpoint_bracket")
    literal = netloc[1:end]
    rest = netloc[end + 1 :]
    try:
        address = ipaddress.IPv6Address(literal)
    except ValueError:
        raise _endpoint_error("endpoint_ip_invalid") from None
    if str(address) != literal:
        raise _endpoint_error("endpoint_not_normalized")
    port = None
    if rest:
        if not rest.startswith(":"):
            raise _endpoint_error("endpoint_bracket")
        port = _parse_port(rest[1:])
    return literal, "ipv6", port


def _parse_host(netloc: str) -> tuple[str, str, int | None]:
    if netloc.startswith("["):
        return _parse_ipv6_host(netloc)
    if "[" in netloc or "]" in netloc:
        raise _endpoint_error("endpoint_bracket")
    host, separator, port_text = netloc.partition(":")
    port = _parse_port(port_text) if separator else None
    if not host:
        raise _endpoint_error("endpoint_host_empty")
    if _IPV4_SHAPE_RE.match(host) is not None:
        try:
            address4 = ipaddress.IPv4Address(host)
        except ValueError:
            raise _endpoint_error("endpoint_ip_invalid") from None
        if str(address4) != host:
            raise _endpoint_error("endpoint_not_normalized")
        return host, "ipv4", port
    # Resolver/URL stacks may interpret inet-style hexadecimal, octal or
    # shortened integer components as IPv4. They must not enter consumer
    # policy disguised as DNS. Canonical dotted decimal was handled above;
    # refuse other all-numeric spellings without DNS or normalization.
    if all(_NUMERIC_IP_COMPONENT_RE.fullmatch(label) for label in host.split(".")):
        raise _endpoint_error("endpoint_ip_invalid")
    if len(host) > MAX_HOSTNAME_CHARS:
        raise _endpoint_error("endpoint_host_too_long")
    for label in host.split("."):
        if not label or len(label) > MAX_DNS_LABEL_CHARS:
            raise _endpoint_error("endpoint_label_length")
        if _DNS_LABEL_RE.match(label) is None:
            raise _endpoint_error("endpoint_label_syntax")
    return host, "dns", port


def _reserved_example(host: str, host_kind: str) -> bool:
    if host_kind == "dns":
        labels = host.split(".")
        if labels[-1] in _RESERVED_TLDS:
            return True
        return ".".join(labels[-2:]) in _RESERVED_DOMAINS
    address = ipaddress.ip_address(host)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _RESERVED_NETWORKS)


def parse_endpoint(value: str) -> EndpointFacts:
    """Parse an endpoint with a real URL/IP/DNS/port parser.

    Rejects a non-HTTP(S) scheme, userinfo, query, fragment, control
    characters, percent-encoded authority delimiters, malformed brackets,
    invalid IP literals, oversized or malformed DNS labels, out-of-range ports
    and dot segments. Any value whose parse would silently normalize to a
    different destination is refused rather than rewritten. Parsing is not a
    transport decision: the returned facts still have to pass the consumer's
    own policy.
    """

    if len(value) > MAX_ENDPOINT_CHARS:
        raise _endpoint_error("endpoint_too_long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in value):
        raise _endpoint_error("endpoint_control_character")
    try:
        split = urlsplit(value)
    except ValueError:
        # The stdlib parser itself refuses the authority (malformed brackets,
        # non-address IPv6 literal). Refusing is the whole point; the reason
        # code stays stable and content-free.
        raise _endpoint_error("endpoint_invalid") from None
    if split.scheme not in {"http", "https"}:
        raise _endpoint_error("endpoint_scheme")
    if split.query or "?" in value:
        raise _endpoint_error("endpoint_query")
    if split.fragment or "#" in value:
        raise _endpoint_error("endpoint_fragment")
    netloc = split.netloc
    if "@" in netloc:
        raise _endpoint_error("endpoint_userinfo")
    if "%" in netloc:
        raise _endpoint_error("endpoint_encoded_delimiter")
    if netloc != netloc.lower():
        raise _endpoint_error("endpoint_not_normalized")
    host, host_kind, port = _parse_host(netloc)
    path = split.path
    if path:
        if not path.startswith("/"):
            raise _endpoint_error("endpoint_path")
        segments = path.split("/")[1:]
        if any(segment in {"", ".", ".."} for segment in segments[:-1]):
            raise _endpoint_error("endpoint_path")
        if segments[-1] in {".", ".."}:
            raise _endpoint_error("endpoint_path")
    if f"{split.scheme}://{netloc}{path}" != value:
        raise _endpoint_error("endpoint_not_normalized")
    return EndpointFacts(
        scheme=split.scheme,
        host=host,
        host_kind=host_kind,
        port=port,
        path=path,
        reserved_example=_reserved_example(host, host_kind),
    )


# --- layer 4: semantics ------------------------------------------------------


def _structural_semantics(nodes: Sequence[Mapping[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for index, node in enumerate(nodes):
        alias = node["alias"]
        if alias in seen:
            findings.append(Finding("semantic_duplicate_alias", f"nodes[{index}].alias"))
        seen.add(alias)
        key_ref = node.get("keyRef")
        if key_ref is not None and key_ref.split(":")[1] != alias:
            findings.append(
                Finding("semantic_key_ref_alias_mismatch", f"nodes[{index}].keyRef")
            )
        endpoint = node.get("endpoint")
        if endpoint is not None:
            try:
                parse_endpoint(endpoint)
            except TopologyError as error:
                findings.append(Finding(error.finding.code, f"nodes[{index}].endpoint"))
    return findings


def _operational_semantics(
    nodes: Sequence[Mapping[str, Any]], context: OperationalContext
) -> tuple[list[Finding], list[Finding]]:
    findings: list[Finding] = []
    notices: list[Finding] = []
    by_alias = {node["alias"]: (index, node) for index, node in enumerate(nodes)}

    if not context.local_alias:
        findings.append(Finding("operational_local_identity_required", "nodes"))
    elif context.local_alias not in by_alias:
        findings.append(Finding("operational_local_identity_absent", "nodes"))
    if not context.selected_aliases:
        findings.append(Finding("operational_subset_required", "nodes"))

    for alias in sorted(context.selected_aliases):
        entry = by_alias.get(alias)
        if entry is None:
            findings.append(Finding("operational_selected_alias_absent", "nodes"))
            continue
        index, node = entry
        location = f"nodes[{index}]"
        if node["enabled"] is not True:
            findings.append(Finding("operational_node_disabled", f"{location}.enabled"))
        findings.extend(_operational_endpoint(node, location, context, notices))
        findings.extend(_operational_key_ref(node, location, context, notices))
    return findings, notices


def _operational_endpoint(
    node: Mapping[str, Any],
    location: str,
    context: OperationalContext,
    notices: list[Finding],
) -> list[Finding]:
    endpoint = node.get("endpoint")
    if endpoint is None:
        # Absent means unavailable for that node, never a guessed default.
        notices.append(Finding("notice_endpoint_absent", f"{location}.endpoint"))
        return []
    field = f"{location}.endpoint"
    try:
        facts = parse_endpoint(endpoint)
    except TopologyError as error:
        return [Finding(error.finding.code, field)]
    if facts.reserved_example:
        return [Finding("operational_endpoint_reserved_example", field)]
    if context.endpoint_policy is None:
        # The consumer's transport/trust policy is an external obligation this
        # validator cannot supply, so the operation is refused, not approved.
        return [Finding("operational_transport_policy_required", field)]
    try:
        permitted = context.endpoint_policy(facts)
    except Exception:
        # Callbacks can fail with private endpoint/keyring details in their
        # exceptions. Only content-free findings cross this public boundary.
        return [Finding("operational_endpoint_policy_failed", field)]
    if permitted is not True:
        return [Finding("operational_endpoint_policy_rejected", field)]
    return []


def _operational_key_ref(
    node: Mapping[str, Any],
    location: str,
    context: OperationalContext,
    notices: list[Finding],
) -> list[Finding]:
    key_ref = node.get("keyRef")
    if key_ref is None:
        notices.append(Finding("notice_key_reference_absent", f"{location}.keyRef"))
        return []
    field = f"{location}.keyRef"
    if context.keyring_resolver is None:
        # A keyRef is a reference: membership, validity, revocation and
        # authorization live in a separately trusted keyring, and the
        # descriptor can never authorize its own reference.
        return [Finding("operational_keyring_authorization_required", field)]
    try:
        authorized = context.keyring_resolver(key_ref)
    except Exception:
        return [Finding("operational_keyring_resolver_failed", field)]
    if authorized is not True:
        return [Finding("operational_key_reference_unauthorized", field)]
    return []


# --- entry points ------------------------------------------------------------


def _validate(payload: bytes, context: OperationalContext | None) -> Report:
    mode = MODE_STRUCTURAL if context is None else MODE_OPERATIONAL
    try:
        document = decode_descriptor(payload)
    except TopologyError as error:
        return Report(mode=mode, findings=(error.finding,))
    structure = validate_structure(document)
    if structure:
        # A structural failure rejects the entire document: no partial use and
        # no synthesized default, so the semantic pass is not attempted.
        return Report(mode=mode, findings=tuple(structure))
    nodes = document["nodes"]
    findings = _structural_semantics(nodes)
    notices: list[Finding] = []
    if context is not None:
        operational, notices = _operational_semantics(nodes, context)
        findings.extend(operational)
    return Report(
        mode=mode,
        findings=tuple(findings),
        notices=tuple(notices),
        document=document if not findings else None,
    )


def validate_structural(payload: bytes) -> Report:
    """Validate syntax, structure and content-independent semantics.

    A passing report means the bytes are a well-formed v1 inventory document.
    It is explicitly not a configuration, deployment or transport check:
    ``Report.operational_ready`` is always false in this mode, and reserved
    example endpoints such as the checked-in ``.invalid`` placeholder are
    accepted here precisely because this mode makes no operational claim.
    """

    return _validate(payload, None)


def validate_operational(payload: bytes, context: OperationalContext) -> Report:
    """Validate everything structural mode does plus caller-trusted semantics.

    Requires a trusted local identity in the inventory and an explicit
    non-empty subset, refuses disabled selected nodes and reserved example
    endpoints, and fails closed when the consumer's transport policy or a
    separately trusted keyring resolver is missing for a selected node.
    Relay, team, wiki and keyring-source routing remain unmodeled; see
    :func:`require_modeled_capability`.
    """

    return _validate(payload, context)


def read_and_validate_structural(
    root_fd: int, relative_path: str, trust: DescriptorTrust
) -> Report:
    """Read under a caller-trusted root, then run :func:`validate_structural`."""

    try:
        payload = read_descriptor(root_fd, relative_path, trust)
    except TopologyError as error:
        return Report(mode=MODE_STRUCTURAL, findings=(error.finding,))
    return validate_structural(payload)


def read_and_validate_operational(
    root_fd: int,
    relative_path: str,
    trust: DescriptorTrust,
    context: OperationalContext,
) -> Report:
    """Read under a caller-trusted root, then run :func:`validate_operational`."""

    try:
        payload = read_descriptor(root_fd, relative_path, trust)
    except TopologyError as error:
        return Report(mode=MODE_OPERATIONAL, findings=(error.finding,))
    return validate_operational(payload, context)


__all__ = [
    "DescriptorTrust",
    "EndpointFacts",
    "Finding",
    "MAX_DESCRIPTOR_BYTES",
    "MAX_JSON_DEPTH",
    "MODELED_CAPABILITIES",
    "MODE_OPERATIONAL",
    "MODE_STRUCTURAL",
    "OperationalContext",
    "Report",
    "SCHEMA_PATH",
    "TopologyError",
    "decode_descriptor",
    "load_schema",
    "parse_endpoint",
    "read_and_validate_operational",
    "read_and_validate_structural",
    "read_descriptor",
    "require_modeled_capability",
    "trusted_root",
    "validate_operational",
    "validate_structural",
    "validate_structure",
]
