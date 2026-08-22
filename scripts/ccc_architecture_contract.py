#!/usr/bin/env python3
"""Validate the versioned ccc-node architecture import contract (#872).

The checker deliberately enforces only declared directional boundaries. New
modules may remain unclassified until a reviewed contract change assigns them;
this keeps the contract from becoming a broad module freeze during #896.
Diagnostics contain rule names and source/import paths only, never file bodies.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from fnmatch import fnmatchcase
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_CONTRACT = Path("architecture/architecture-contract-v1.json")


class ContractError(ValueError):
    """The contract is invalid or cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class PythonRoot:
    path: str
    package: str


@dataclass(frozen=True, slots=True)
class Layer:
    name: str
    module_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportRule:
    name: str
    from_layers: tuple[str, ...]
    forbid_layers: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ArchitectureContract:
    schema_version: int
    python_roots: tuple[PythonRoot, ...]
    layers: tuple[Layer, ...]
    rules: tuple[ImportRule, ...]


@dataclass(frozen=True, slots=True)
class ModuleSource:
    module: str
    relative_path: str
    path: Path
    is_package: bool


@dataclass(frozen=True, slots=True)
class ImportEdge:
    source: ModuleSource
    target: str
    line: int


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    source_path: str
    source_module: str
    target_module: str
    source_layer: str
    target_layer: str
    line: int

    def render(self) -> str:
        return (
            f"{self.rule}: {self.source_path}:{self.line}: "
            f"{self.source_module} imports {self.target_module} "
            f"({self.source_layer} -> {self.target_layer})"
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    module_count: int
    classified_count: int
    import_count: int
    violations: tuple[Violation, ...]


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{location} must be an object")
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{location} must be a non-empty string")
    return value


def _string_tuple(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{location} must be a non-empty string array")
    return tuple(_string(item, f"{location}[{index}]") for index, item in enumerate(value))


def _object_list(value: Any, location: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{location} must be a non-empty object array")
    return [_mapping(item, f"{location}[{index}]") for index, item in enumerate(value)]


def _safe_relative_path(value: Any, location: str) -> str:
    raw = _string(value, location)
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw in {".", ""}:
        raise ContractError(f"{location} must be a repository-relative path without '..'")
    return path.as_posix()


def load_contract(path: Path) -> ArchitectureContract:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read contract {path}: {exc}") from exc
    doc = _mapping(raw, "contract")
    version = doc.get("schema_version")
    if version != 1:
        raise ContractError(f"schema_version must be 1 (got {version!r})")

    roots = tuple(
        PythonRoot(
            path=_safe_relative_path(item.get("path"), f"python_roots[{index}].path"),
            package=_string(item.get("package"), f"python_roots[{index}].package"),
        )
        for index, item in enumerate(_object_list(doc.get("python_roots"), "python_roots"))
    )
    layers = tuple(
        Layer(
            name=_string(item.get("name"), f"layers[{index}].name"),
            module_patterns=_string_tuple(
                item.get("module_patterns"), f"layers[{index}].module_patterns"
            ),
        )
        for index, item in enumerate(_object_list(doc.get("layers"), "layers"))
    )
    rules = tuple(
        ImportRule(
            name=_string(item.get("name"), f"rules[{index}].name"),
            from_layers=_string_tuple(
                item.get("from_layers"), f"rules[{index}].from_layers"
            ),
            forbid_layers=_string_tuple(
                item.get("forbid_layers"), f"rules[{index}].forbid_layers"
            ),
            reason=_string(item.get("reason"), f"rules[{index}].reason"),
        )
        for index, item in enumerate(_object_list(doc.get("rules"), "rules"))
    )

    _require_unique((root.path for root in roots), "python root path")
    layer_names = {layer.name for layer in layers}
    if len(layer_names) != len(layers):
        raise ContractError("layer names must be unique")
    _require_unique((rule.name for rule in rules), "rule name")
    for rule in rules:
        unknown = (set(rule.from_layers) | set(rule.forbid_layers)) - layer_names
        if unknown:
            raise ContractError(f"rule {rule.name!r} references unknown layers: {sorted(unknown)}")
    return ArchitectureContract(version, roots, layers, rules)


def _require_unique(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractError(f"duplicate {label}: {value}")
        seen.add(value)


def _module_name(root: PythonRoot, relative: Path) -> str:
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    suffix = ".".join(parts)
    return root.package if not suffix else f"{root.package}.{suffix}"


def discover_modules(repo_root: Path, contract: ArchitectureContract) -> tuple[ModuleSource, ...]:
    modules: list[ModuleSource] = []
    resolved_repo = repo_root.resolve()
    for root in contract.python_roots:
        source_root = (repo_root / root.path).resolve()
        if not source_root.is_relative_to(resolved_repo):
            raise ContractError(f"python root escapes repository: {root.path}")
        if not source_root.is_dir():
            raise ContractError(f"python root does not exist: {root.path}")
        for path in sorted(source_root.rglob("*.py")):
            relative = path.relative_to(source_root)
            repo_relative = path.relative_to(repo_root).as_posix()
            modules.append(
                ModuleSource(
                    _module_name(root, relative),
                    repo_relative,
                    path,
                    relative.name == "__init__.py",
                )
            )
    _require_unique((module.module for module in modules), "discovered module")
    return tuple(modules)


def classify_modules(
    modules: Sequence[ModuleSource], contract: ArchitectureContract
) -> dict[str, str]:
    classified: dict[str, str] = {}
    matched_layers: set[str] = set()
    for module in modules:
        matches = [
            layer.name
            for layer in contract.layers
            if any(fnmatchcase(module.module, pattern) for pattern in layer.module_patterns)
        ]
        if len(matches) > 1:
            raise ContractError(f"module {module.module!r} matches multiple layers: {matches}")
        if matches:
            classified[module.module] = matches[0]
            matched_layers.add(matches[0])
    empty = sorted(layer.name for layer in contract.layers if layer.name not in matched_layers)
    if empty:
        raise ContractError(f"layers match no repository modules: {empty}")
    return classified


def _resolved_imports(
    node: ast.AST,
    source_module: str,
    source_is_package: bool,
    known_modules: set[str],
) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if not isinstance(node, ast.ImportFrom):
        return set()

    if node.level:
        package = source_module if source_is_package else source_module.rpartition(".")[0]
        if not package:
            return set()
        relative_name = "." * node.level + (node.module or "")
        try:
            base = importlib.util.resolve_name(relative_name, package)
        except (ImportError, ValueError):
            return set()
    else:
        base = node.module or ""
    if not base:
        return set()

    targets = {base}
    for alias in node.names:
        candidate = f"{base}.{alias.name}"
        if candidate in known_modules:
            targets.add(candidate)
    return targets


def collect_imports(modules: Sequence[ModuleSource]) -> tuple[ImportEdge, ...]:
    known_modules = {module.module for module in modules}
    edges: list[ImportEdge] = []
    for source in modules:
        try:
            tree = ast.parse(source.path.read_text(encoding="utf-8"), filename=source.relative_path)
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ContractError(f"cannot parse {source.relative_path}: {exc}") from exc
        for node in ast.walk(tree):
            for target in sorted(
                _resolved_imports(node, source.module, source.is_package, known_modules)
            ):
                edges.append(ImportEdge(source, target, getattr(node, "lineno", 0)))
    return tuple(edges)


def validate(repo_root: Path, contract_path: Path) -> ValidationResult:
    contract = load_contract(contract_path)
    modules = discover_modules(repo_root, contract)
    classified = classify_modules(modules, contract)
    edges = collect_imports(modules)
    violations: list[Violation] = []
    for rule in contract.rules:
        for edge in edges:
            source_layer = classified.get(edge.source.module)
            target_layer = classified.get(edge.target)
            if source_layer not in rule.from_layers or target_layer not in rule.forbid_layers:
                continue
            violations.append(
                Violation(
                    rule=rule.name,
                    source_path=edge.source.relative_path,
                    source_module=edge.source.module,
                    target_module=edge.target,
                    source_layer=source_layer,
                    target_layer=target_layer,
                    line=edge.line,
                )
            )
    return ValidationResult(
        module_count=len(modules),
        classified_count=len(classified),
        import_count=len(edges),
        violations=tuple(
            sorted(violations, key=lambda item: (item.rule, item.source_path, item.line, item.target_module))
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    contract_path = args.contract
    if not contract_path.is_absolute():
        contract_path = repo_root / contract_path
    try:
        result = validate(repo_root, contract_path)
    except ContractError as exc:
        print(f"architecture-contract: invalid: {exc}", file=sys.stderr)
        return 2
    if result.violations:
        for violation in result.violations:
            print(f"architecture-contract: FAIL {violation.render()}", file=sys.stderr)
        return 1
    print(
        "architecture-contract: ok "
        f"modules={result.module_count} classified={result.classified_count} "
        f"imports={result.import_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
