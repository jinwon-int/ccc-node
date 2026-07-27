"""Guard: every schema passed to `codex exec --output-schema` must satisfy the
OpenAI structured-output constraints (#760).

These files are never validated against the real API in CI, so a schema the API
rejects merges cleanly and only fails at runtime — as a `codex_distill_nonzero_exit`
with the provider's stderr discarded, which is close to undiagnosable. In #760 a
single `oneOf` in the skill-candidate v2 schema broke the Codex collector on every
Codex node with an HTTP 400 (`'oneOf' is not permitted`) before the model ran at all.

Static and hermetic: no binary, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Only schemas handed to `--output-schema`. Others (e.g. the agent-cron task
# store) are validated locally with jsonschema and are not bound by these rules.
OUTPUT_SCHEMAS = sorted((REPO_ROOT / "schemas").glob("codex-*.schema.json"))

# Rejected outright by the structured-output validator. `anyOf` is the supported
# union keyword; a `$ref` tagged union converts across with no practical loss.
FORBIDDEN_KEYWORDS = ("oneOf", "not")


def _walk(node: object, path: str = "root"):
    """Yield (path, dict) for every mapping in the document."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_output_schema_set_is_not_empty() -> None:
    # A glob that silently matches nothing would make every check below vacuous.
    assert OUTPUT_SCHEMAS, "no codex-*.schema.json found under schemas/"


@pytest.mark.parametrize("schema_path", OUTPUT_SCHEMAS, ids=lambda p: p.name)
def test_no_forbidden_keywords(schema_path: Path) -> None:
    offenders = [
        f"{keyword} @ {path}"
        for path, node in _walk(_load(schema_path))
        for keyword in FORBIDDEN_KEYWORDS
        if keyword in node
    ]
    assert not offenders, (
        f"{schema_path.name} uses keywords the structured-output validator "
        f"rejects (use anyOf instead of oneOf): {offenders}"
    )


@pytest.mark.parametrize("schema_path", OUTPUT_SCHEMAS, ids=lambda p: p.name)
def test_objects_are_closed_and_fully_required(schema_path: Path) -> None:
    """Strict mode: every object closes extras and requires every property."""
    offenders: list[str] = []
    for path, node in _walk(_load(schema_path)):
        properties = node.get("properties")
        if not isinstance(properties, dict):
            continue
        if node.get("additionalProperties") is not False:
            offenders.append(f"additionalProperties is not false @ {path}")
        missing = sorted(set(properties) - set(node.get("required") or ()))
        if missing:
            offenders.append(f"properties missing from required {missing} @ {path}")
    assert not offenders, f"{schema_path.name}: {offenders}"


@pytest.mark.parametrize("schema_path", OUTPUT_SCHEMAS, ids=lambda p: p.name)
def test_local_refs_resolve(schema_path: Path) -> None:
    """A dangling $ref fails the same way — at request time, not in CI."""
    document = _load(schema_path)
    defs = document.get("$defs") or {}
    dangling = [
        f"{node['$ref']} @ {path}"
        for path, node in _walk(document)
        if isinstance(node.get("$ref"), str)
        and node["$ref"].startswith("#/$defs/")
        and node["$ref"].removeprefix("#/$defs/") not in defs
    ]
    assert not dangling, f"{schema_path.name} has unresolvable $refs: {dangling}"
