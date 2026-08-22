#!/usr/bin/env python3
"""Validate the typed external side-effect and recovery contract (#872).

The contract is metadata only. Diagnostics name operations, rules, and tracked
paths, but never print source bodies, runtime payloads, or credential values.
Recovery drills use an in-memory fake sink and perform no external calls.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence, TypeVar


DEFAULT_CONTRACT = Path("architecture/side-effect-contract-v1.json")
DEFAULT_DOCUMENT = Path("docs/side-effect-contract.md")
GENERATED_BEGIN = "<!-- ccc-side-effect-contract:begin -->"
GENERATED_END = "<!-- ccc-side-effect-contract:end -->"
OPERATION_PATTERN = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"
OPERATION_RE = re.compile(rf"^{OPERATION_PATTERN}$")
MARKER_RE = re.compile(rf"^[ \t]*# ccc-side-effect: ({OPERATION_PATTERN})[ \t]*$")
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_BYTES = 256 * 1024
MAX_TEXT_CHARS = 320
SENSITIVE_VALUE_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{12,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class ContractError(ValueError):
    """The side-effect contract is invalid or cannot be checked safely."""


class IdempotencyMode(str, Enum):
    NATIVE = "native"
    LOCAL_LEDGER = "local-ledger"
    NONE = "none"


class RetryClass(str, Enum):
    SAFE = "safe"
    CONDITIONAL = "conditional"
    MANUAL_ONLY = "manual-only"


class ReconcileMode(str, Enum):
    QUERY = "query"
    RECEIPT = "receipt"
    MANUAL = "manual"
    NONE = "none"


class CompensationMode(str, Enum):
    DELETE = "delete"
    EDIT = "edit"
    REVERSE = "reverse"
    RESTORE_SNAPSHOT = "restore-snapshot"
    NONE = "none"


class RegistrationKind(str, Enum):
    PYTHON_SYMBOL = "python-symbol"
    SHELL_MAIN = "shell-main"


class RecoveryBoundary(str, Enum):
    BEFORE_INTENT = "before_intent"
    AFTER_INTENT_BEFORE_CALL = "after_intent_before_call"
    AFTER_EXTERNAL_SUCCESS_BEFORE_ACK = "after_external_success_before_ack"
    AFTER_ACK_BEFORE_TERMINAL = "after_ack_before_terminal"
    DUPLICATE_RESTART_REPLAY = "duplicate_restart_replay"


class RecoveryAction(str, Enum):
    SAFE_REPLAY = "safe-replay"
    RECONCILE = "reconcile"
    MANUAL_REVIEW = "manual-review"
    COMPENSATE = "compensate"
    TERMINAL_FAILURE = "terminal-failure"


RECOVERY_BOUNDARIES = tuple(RecoveryBoundary)


@dataclass(frozen=True, slots=True)
class Registration:
    kind: RegistrationKind
    path: str
    symbol: str


@dataclass(frozen=True, slots=True)
class SideEffectOperation:
    operation: str
    owner: str
    registration: Registration
    external: bool
    idempotency: IdempotencyMode
    idempotency_key: str
    retry_class: RetryClass
    ambiguous_window: str
    reconcile: ReconcileMode
    compensation: CompensationMode
    audit_surface: str
    approval_boundary: str
    recovery: Mapping[RecoveryBoundary, RecoveryAction]


@dataclass(frozen=True, slots=True)
class SideEffectContract:
    schema_version: int
    registration_roots: tuple[str, ...]
    operations: tuple[SideEffectOperation, ...]


@dataclass(frozen=True, slots=True)
class DrillObservation:
    operation: str
    boundary: RecoveryBoundary
    action: RecoveryAction
    attempts: int
    unique_effects: int
    intent_recorded: bool
    ack_recorded: bool
    terminal_projected: bool


@dataclass(frozen=True, slots=True)
class ValidationResult:
    operation_count: int
    marker_count: int
    drill_count: int


class _DuplicateKey(ValueError):
    pass


EnumType = TypeVar("EnumType", bound=Enum)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{location} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise ContractError(f"{location} fields are invalid (missing={missing}, extra={extra})")


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractError(f"{location} must be a non-empty trimmed string")
    if len(value) > MAX_TEXT_CHARS or any(ord(char) < 32 for char in value):
        raise ContractError(f"{location} must be bounded single-line metadata")
    if SENSITIVE_VALUE_RE.search(value):
        raise ContractError(f"{location} resembles a credential value")
    return value


def _enum(enum_type: type[EnumType], value: Any, location: str) -> EnumType:
    text = _text(value, location)
    try:
        return enum_type(text)
    except ValueError:
        allowed = sorted(str(item.value) for item in enum_type)
        raise ContractError(f"{location} must be one of {allowed}") from None


def _safe_path(value: Any, location: str) -> str:
    text = _text(value, location)
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or text in {".", ""}:
        raise ContractError(f"{location} must be a safe repository-relative path")
    return path.as_posix()


def _object_list(value: Any, location: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{location} must be a non-empty object array")
    return [_mapping(item, f"{location}[{index}]") for index, item in enumerate(value)]


def _require_unique(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractError(f"duplicate {label}: {value}")
        seen.add(value)


def _parse_registration(value: Any, location: str) -> Registration:
    data = _mapping(value, location)
    _exact_fields(data, {"kind", "path", "symbol"}, location)
    kind = _enum(RegistrationKind, data.get("kind"), f"{location}.kind")
    path = _safe_path(data.get("path"), f"{location}.path")
    symbol = _text(data.get("symbol"), f"{location}.symbol")
    if kind is RegistrationKind.PYTHON_SYMBOL:
        if PurePosixPath(path).suffix != ".py" or not all(
            part.isidentifier() for part in symbol.split(".")
        ):
            raise ContractError(f"{location} has an invalid Python symbol registration")
    elif PurePosixPath(path).suffix != ".sh" or symbol != "<top-level>":
        raise ContractError(f"{location} has an invalid shell-main registration")
    return Registration(kind, path, symbol)


def _parse_recovery(value: Any, location: str) -> Mapping[RecoveryBoundary, RecoveryAction]:
    data = _mapping(value, location)
    expected = {boundary.value for boundary in RECOVERY_BOUNDARIES}
    _exact_fields(data, expected, location)
    return {
        boundary: _enum(RecoveryAction, data.get(boundary.value), f"{location}.{boundary.value}")
        for boundary in RECOVERY_BOUNDARIES
    }


def _parse_operation(value: Mapping[str, Any], index: int) -> SideEffectOperation:
    location = f"operations[{index}]"
    fields = {
        "operation",
        "owner",
        "registration",
        "external",
        "idempotency",
        "idempotency_key",
        "retry_class",
        "ambiguous_window",
        "reconcile",
        "compensation",
        "audit_surface",
        "approval_boundary",
        "recovery",
    }
    _exact_fields(value, fields, location)
    operation = _text(value.get("operation"), f"{location}.operation")
    if not OPERATION_RE.fullmatch(operation):
        raise ContractError(f"{location}.operation has an invalid name")
    if value.get("external") is not True:
        raise ContractError(f"{location}.external must be true")
    return SideEffectOperation(
        operation=operation,
        owner=_text(value.get("owner"), f"{location}.owner"),
        registration=_parse_registration(value.get("registration"), f"{location}.registration"),
        external=True,
        idempotency=_enum(IdempotencyMode, value.get("idempotency"), f"{location}.idempotency"),
        idempotency_key=_text(value.get("idempotency_key"), f"{location}.idempotency_key"),
        retry_class=_enum(RetryClass, value.get("retry_class"), f"{location}.retry_class"),
        ambiguous_window=_text(value.get("ambiguous_window"), f"{location}.ambiguous_window"),
        reconcile=_enum(ReconcileMode, value.get("reconcile"), f"{location}.reconcile"),
        compensation=_enum(
            CompensationMode, value.get("compensation"), f"{location}.compensation"
        ),
        audit_surface=_text(value.get("audit_surface"), f"{location}.audit_surface"),
        approval_boundary=_text(
            value.get("approval_boundary"), f"{location}.approval_boundary"
        ),
        recovery=_parse_recovery(value.get("recovery"), f"{location}.recovery"),
    )


def expected_recovery_action(
    operation: SideEffectOperation, boundary: RecoveryBoundary
) -> RecoveryAction:
    if boundary in {
        RecoveryBoundary.BEFORE_INTENT,
        RecoveryBoundary.AFTER_INTENT_BEFORE_CALL,
        RecoveryBoundary.AFTER_ACK_BEFORE_TERMINAL,
    }:
        return RecoveryAction.SAFE_REPLAY
    if operation.idempotency is IdempotencyMode.NATIVE:
        return RecoveryAction.SAFE_REPLAY
    if operation.reconcile in {ReconcileMode.QUERY, ReconcileMode.RECEIPT}:
        return RecoveryAction.RECONCILE
    if operation.reconcile is ReconcileMode.MANUAL:
        return RecoveryAction.MANUAL_REVIEW
    if operation.compensation is not CompensationMode.NONE:
        return RecoveryAction.COMPENSATE
    return RecoveryAction.TERMINAL_FAILURE


def _validate_policy(operation: SideEffectOperation) -> None:
    if operation.idempotency is IdempotencyMode.NONE:
        if operation.idempotency_key != "none":
            raise ContractError(f"{operation.operation}: idempotency none requires key none")
        if operation.retry_class is RetryClass.SAFE:
            raise ContractError(f"{operation.operation}: non-idempotent retry cannot be safe")
        if (
            operation.reconcile is ReconcileMode.NONE
            and operation.compensation is CompensationMode.NONE
        ):
            raise ContractError(
                f"{operation.operation}: non-idempotent effect needs recovery handoff"
            )
    elif operation.idempotency_key == "none":
        raise ContractError(f"{operation.operation}: idempotent effect requires a stable key")
    if operation.ambiguous_window == "none":
        raise ContractError(f"{operation.operation}: external effect needs an ambiguous window")
    for boundary in RECOVERY_BOUNDARIES:
        expected = expected_recovery_action(operation, boundary)
        if operation.recovery[boundary] is not expected:
            raise ContractError(
                f"{operation.operation}: {boundary.value} must use {expected.value}"
            )


def load_contract(path: Path) -> SideEffectContract:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ContractError("side-effect contract must be a regular non-symlink file")
        if metadata.st_size > MAX_CONTRACT_BYTES:
            raise ContractError("side-effect contract exceeds its size bound")
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except ContractError:
        raise
    except _DuplicateKey as exc:
        raise ContractError(f"contract contains duplicate key: {exc}") from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read side-effect contract {path}") from exc
    data = _mapping(raw, "contract")
    _exact_fields(data, {"schema_version", "registration_roots", "operations"}, "contract")
    if data.get("schema_version") != 1:
        raise ContractError("schema_version must be 1")
    roots_raw = data.get("registration_roots")
    if not isinstance(roots_raw, list) or not roots_raw:
        raise ContractError("registration_roots must be a non-empty path array")
    roots = tuple(
        _safe_path(item, f"registration_roots[{index}]")
        for index, item in enumerate(roots_raw)
    )
    _require_unique(roots, "registration root")
    operations = tuple(
        _parse_operation(item, index)
        for index, item in enumerate(_object_list(data.get("operations"), "operations"))
    )
    _require_unique((item.operation for item in operations), "operation")
    for operation in operations:
        registration_path = PurePosixPath(operation.registration.path)
        if not any(
            registration_path.is_relative_to(PurePosixPath(root)) for root in roots
        ):
            raise ContractError(
                f"{operation.operation}: registration path is outside registration roots"
            )
        _validate_policy(operation)
    return SideEffectContract(1, roots, operations)


def _tracked_regular_file(repo_root: Path, relative: PurePosixPath) -> Path:
    candidate = repo_root
    for part in relative.parts:
        candidate /= part
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            raise ContractError(f"tracked registration source is unavailable: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise ContractError(f"tracked registration source is a symlink: {relative}")
    if not stat.S_ISREG(mode):
        raise ContractError(f"tracked registration source is not regular: {relative}")
    return candidate


def _tracked_sources(repo_root: Path, roots: Sequence[str]) -> Mapping[str, Path]:
    resolved_repo = repo_root.resolve()
    for root in roots:
        source_root = (resolved_repo / root).resolve()
        if not source_root.is_relative_to(resolved_repo) or not source_root.is_dir():
            raise ContractError(f"registration root is unsafe or missing: {root}")
    command = [
        "git",
        "-C",
        str(resolved_repo),
        "ls-files",
        "-z",
        "--stage",
        "--",
        *(f":(top,literal){root}" for root in roots),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ContractError("cannot enumerate tracked registration sources") from exc
    if completed.returncode != 0:
        raise ContractError(
            f"cannot enumerate tracked registration sources (git exit {completed.returncode})"
        )
    sources: dict[str, Path] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ContractError("git returned malformed registration metadata")
        try:
            text = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractError("tracked registration path is not valid UTF-8") from exc
        relative = PurePosixPath(text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ContractError("git returned an unsafe registration path")
        if relative.suffix not in {".py", ".sh"}:
            continue
        if fields[0] == b"120000":
            raise ContractError(f"tracked registration source is a symlink: {relative}")
        if fields[0] not in {b"100644", b"100755"}:
            raise ContractError(f"tracked registration source is not regular: {relative}")
        path = _tracked_regular_file(resolved_repo, relative)
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_repo):
            raise ContractError(f"tracked registration source escapes repository: {relative}")
        if relative.as_posix() in sources:
            raise ContractError(f"duplicate tracked registration source: {relative}")
        sources[relative.as_posix()] = resolved
    return sources


def _source_text(path: Path, relative: str) -> str:
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ContractError(f"tracked registration source is too large: {relative}")
        return path.read_text(encoding="utf-8")
    except ContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read tracked registration source: {relative}") from exc


def _markers(sources: Mapping[str, Path]) -> Mapping[str, str]:
    markers: dict[str, str] = {}
    for relative in sorted(sources):
        for line in _source_text(sources[relative], relative).splitlines():
            match = MARKER_RE.fullmatch(line)
            if not match:
                continue
            operation = match.group(1)
            if operation in markers:
                raise ContractError(f"duplicate registered operation marker: {operation}")
            markers[operation] = relative
    return markers


def _validate_python_symbol(source: str, relative: str, symbol: str) -> None:
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        raise ContractError(f"cannot parse registered Python source: {relative}") from exc
    nodes: Sequence[ast.stmt] = tree.body
    for index, part in enumerate(symbol.split(".")):
        matches = [
            node
            for node in nodes
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == part
        ]
        if len(matches) != 1:
            raise ContractError(f"registered Python symbol is missing: {relative}::{symbol}")
        match = matches[0]
        if index < len(symbol.split(".")) - 1:
            if not isinstance(match, ast.ClassDef):
                raise ContractError(f"registered Python symbol is invalid: {relative}::{symbol}")
            nodes = match.body


def validate_registrations(repo_root: Path, contract: SideEffectContract) -> int:
    sources = _tracked_sources(repo_root, contract.registration_roots)
    markers = _markers(sources)
    expected = {operation.operation for operation in contract.operations}
    unknown = sorted(set(markers) - expected)
    missing = sorted(expected - set(markers))
    if unknown:
        raise ContractError(f"registered operation missing from contract: {unknown[0]}")
    if missing:
        raise ContractError(f"contract operation missing source marker: {missing[0]}")
    for operation in contract.operations:
        registration = operation.registration
        if markers[operation.operation] != registration.path:
            raise ContractError(f"{operation.operation}: source marker path does not match contract")
        path = sources.get(registration.path)
        if path is None:
            raise ContractError(f"{operation.operation}: registration source is not tracked")
        source = _source_text(path, registration.path)
        if registration.kind is RegistrationKind.PYTHON_SYMBOL:
            _validate_python_symbol(source, registration.path, registration.symbol)
        elif not source.startswith("#!/usr/bin/env bash\n"):
            raise ContractError(f"{operation.operation}: shell-main source must use bash")
    return len(markers)


class _FakeSink:
    def __init__(self, native_idempotency: bool) -> None:
        self.native_idempotency = native_idempotency
        self.attempts = 0
        self.unique_effects = 0
        self._keys: set[str] = set()

    def call(self, key: str) -> None:
        self.attempts += 1
        if self.native_idempotency and key in self._keys:
            return
        self._keys.add(key)
        self.unique_effects += 1

    def compensate(self) -> None:
        if self.unique_effects:
            self.unique_effects -= 1


def _simulate_boundary(
    operation: SideEffectOperation, boundary: RecoveryBoundary
) -> DrillObservation:
    sink = _FakeSink(operation.idempotency is IdempotencyMode.NATIVE)
    key = f"drill:{operation.operation}"
    intent_recorded = boundary is not RecoveryBoundary.BEFORE_INTENT
    acked = False
    if boundary in {
        RecoveryBoundary.AFTER_EXTERNAL_SUCCESS_BEFORE_ACK,
        RecoveryBoundary.AFTER_ACK_BEFORE_TERMINAL,
        RecoveryBoundary.DUPLICATE_RESTART_REPLAY,
    }:
        sink.call(key)
    if boundary is RecoveryBoundary.AFTER_ACK_BEFORE_TERMINAL:
        acked = True
    action = expected_recovery_action(operation, boundary)
    if action is RecoveryAction.SAFE_REPLAY and not acked:
        intent_recorded = True
        sink.call(key)
        acked = True
    elif action is RecoveryAction.RECONCILE:
        acked = True
    elif action is RecoveryAction.COMPENSATE:
        sink.compensate()
        acked = True
    if sink.unique_effects > 1:
        raise ContractError(f"{operation.operation}: recovery drill duplicated an external effect")
    return DrillObservation(
        operation.operation,
        boundary,
        action,
        sink.attempts,
        sink.unique_effects,
        intent_recorded,
        acked,
        True,
    )


def run_recovery_drills(contract: SideEffectContract) -> tuple[DrillObservation, ...]:
    observations = tuple(
        _simulate_boundary(operation, boundary)
        for operation in contract.operations
        for boundary in RECOVERY_BOUNDARIES
    )
    for observation in observations:
        operation = next(
            item for item in contract.operations if item.operation == observation.operation
        )
        if operation.recovery[observation.boundary] is not observation.action:
            raise ContractError(
                f"{observation.operation}: recovery drill action drift at "
                f"{observation.boundary.value}"
            )
    return observations


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_markdown(contract: SideEffectContract) -> str:
    lines = [
        "### External-effect inventory",
        "",
        "| Operation | Owner | Idempotency / key | Retry | Ambiguous window | Reconcile | Compensation | Audit | Approval boundary | Implementation |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in contract.operations:
        registration = item.registration
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    f"`{item.operation}`",
                    item.owner,
                    f"{item.idempotency.value}: `{item.idempotency_key}`",
                    item.retry_class.value,
                    item.ambiguous_window,
                    item.reconcile.value,
                    item.compensation.value,
                    item.audit_surface,
                    item.approval_boundary,
                    f"`{registration.path}::{registration.symbol}`",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "### Deterministic recovery matrix",
            "",
            "| Operation | Before intent | Intent → call | Success → ACK | ACK → terminal | Duplicate/restart |",
            "|---|---|---|---|---|---|",
        ]
    )
    for item in contract.operations:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{item.operation}`",
                    *(item.recovery[boundary].value for boundary in RECOVERY_BOUNDARIES),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def generated_document_block(contract: SideEffectContract) -> str:
    return f"{GENERATED_BEGIN}\n{render_markdown(contract)}\n{GENERATED_END}"


def verify_document(path: Path, contract: SideEffectContract) -> None:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ContractError("side-effect document must be a regular non-symlink file")
        if metadata.st_size > MAX_CONTRACT_BYTES:
            raise ContractError("side-effect document exceeds its size bound")
        text = path.read_text(encoding="utf-8")
    except ContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read side-effect document: {path}") from exc
    if text.count(GENERATED_BEGIN) != 1 or text.count(GENERATED_END) != 1:
        raise ContractError("side-effect document must contain one generated block")
    start = text.index(GENERATED_BEGIN)
    end = text.index(GENERATED_END, start) + len(GENERATED_END)
    if text[start:end] != generated_document_block(contract):
        raise ContractError("generated side-effect document is out of date")


def validate(repo_root: Path, contract_path: Path, document_path: Path) -> ValidationResult:
    contract = load_contract(contract_path)
    marker_count = validate_registrations(repo_root, contract)
    drills = run_recovery_drills(contract)
    verify_document(document_path, contract)
    return ValidationResult(len(contract.operations), marker_count, len(drills))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--render-document", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    contract_path = args.contract
    document_path = args.document
    if not contract_path.is_absolute():
        contract_path = repo_root / contract_path
    if not document_path.is_absolute():
        document_path = repo_root / document_path
    try:
        contract = load_contract(contract_path)
        marker_count = validate_registrations(repo_root, contract)
        drills = run_recovery_drills(contract)
        if args.render_document:
            print(generated_document_block(contract))
            return 0
        verify_document(document_path, contract)
    except ContractError as exc:
        print(f"side-effect-contract: invalid: {exc}", file=sys.stderr)
        return 2
    print(
        "side-effect-contract: ok "
        f"operations={len(contract.operations)} markers={marker_count} drills={len(drills)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
