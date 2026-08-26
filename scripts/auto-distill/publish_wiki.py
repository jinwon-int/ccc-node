#!/usr/bin/env python3
"""Stage one human-gated Wiki PR from the full TM-2380 canary roster.

The extractor deliberately writes node-local AUTO.md files.  This collector is
run from one designated node: it reads every explicitly mapped output, merges
unseen stable keys into one Wiki worktree, and can invoke ``wiki-agent pr``
once.  It never guesses files by glob and never treats a read failure as an
empty node.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Sequence


ROSTER = (
    "seoseo",
    "dungae",
    "sogyo",
    "nosuk",
    "bangtong",
    "yukson",
    "soonwook",
    "gwakga",
    "jingun",
    "gongyung",
    "daegyo",
)
NODE_RE = re.compile(r"^[a-z][a-z0-9-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
HEADER_RE = re.compile(
    r"\A# \[DOC-auto-([a-z][a-z0-9-]*)\] "
    r"([a-z][a-z0-9-]*) AUTO — 자동 승격 후보 \(auto-distill\)\s*$",
    re.M,
)
KEY_RE = re.compile(r"\*\*키\*\*: `([0-9a-f]{12})`")
STATUS_RE = re.compile(
    r"\*\*상태\*\*: `(?:unverified|needs-review|promoted|fix-citation|discarded)`"
)
PIPELINE_RE = re.compile(r"\*\*파이프라인\*\*: `v[0-9]+`")
CONFLICT_RE = re.compile(r"(?m)^(?:<{7}(?: |$)|={7}$|>{7}(?: |$)|\|{7}(?: |$))")
TOKEN_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|github"
    r"_pat_[A-Za-z0-9_]{20,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{32,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[baprs]-[0-9A-Za-z-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
    r"|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
    r"|\b01[016789][-. ]?[0-9]{3,4}[-. ]?[0-9]{4}\b)"
)
ASSIGN_RE = re.compile(
    r"(?:password|passwd|api[_-]?key|secret|access[_-]?token|client[_-]?secret|bearer)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9_+/=.-]{16,}",
    re.I,
)


class PublishError(RuntimeError):
    """A fail-closed publication error safe to print without candidate bodies."""


@dataclass(frozen=True)
class SourceSpec:
    node: str
    kind: str
    value: str | None


@dataclass(frozen=True)
class AutoDocument:
    prefix: str
    blocks: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PlanRow:
    node: str
    source_count: int
    existing_count: int
    appended: int
    skipped_existing: int
    target: Path | None
    content: str | None
    declared_empty: bool = False


def parse_mapping(raw: str, option: str) -> tuple[str, str]:
    if "=" not in raw:
        raise PublishError(f"{option} requires NODE=VALUE")
    node, value = raw.split("=", 1)
    if not NODE_RE.fullmatch(node) or node not in ROSTER:
        raise PublishError(f"{option} has unknown node: {node or '<empty>'}")
    if not value:
        raise PublishError(f"{option} has an empty value for node {node}")
    return node, value


def build_specs(args: argparse.Namespace) -> dict[str, SourceSpec]:
    specs: dict[str, SourceSpec] = {}

    def add(spec: SourceSpec) -> None:
        if spec.node in specs:
            raise PublishError(f"node declared more than once: {spec.node}")
        specs[spec.node] = spec

    for raw in args.local:
        node, value = parse_mapping(raw, "--local")
        add(SourceSpec(node, "local", value))
    for raw in args.remote:
        node, value = parse_mapping(raw, "--remote")
        if not HOST_RE.fullmatch(value) or value.startswith("-"):
            raise PublishError(f"--remote has unsafe SSH host for node {node}")
        add(SourceSpec(node, "remote", value))
    for node in args.empty:
        if not NODE_RE.fullmatch(node) or node not in ROSTER:
            raise PublishError(f"--empty has unknown node: {node}")
        add(SourceSpec(node, "empty", None))

    missing = [node for node in ROSTER if node not in specs]
    if missing:
        raise PublishError("exact 11-node roster required; missing: " + ", ".join(missing))
    return specs


def read_source(spec: SourceSpec, ssh_bin: str, timeout: int) -> str | None:
    if spec.kind == "empty":
        return None
    if spec.kind == "local":
        path = Path(spec.value or "").expanduser()
        if not path.is_file() or path.is_symlink():
            raise PublishError(
                f"local output is missing, not regular, or a symlink: node={spec.node}"
            )
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PublishError(f"cannot read local output: node={spec.node}: {exc}") from exc

    remote_path = f"$HOME/.hermes/logs/auto-{spec.node}.md"
    command = f'cat "{remote_path}"'
    try:
        result = subprocess.run(
            [ssh_bin, "-o", "BatchMode=yes", "--", spec.value or "", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"SSH read failed: node={spec.node}: {exc}") from exc
    if result.returncode != 0:
        raise PublishError(
            f"SSH output read failed: node={spec.node} host={spec.value} rc={result.returncode}; "
            "declare --empty only after separately verifying that the node has no candidates"
        )
    return result.stdout


def split_document(text: str, expected_node: str, label: str) -> AutoDocument:
    if CONFLICT_RE.search(text):
        raise PublishError(f"merge-conflict marker detected: node={expected_node} source={label}")
    if TOKEN_RE.search(text) or ASSIGN_RE.search(text):
        raise PublishError(f"secret-like content detected: node={expected_node} source={label}")

    header = HEADER_RE.search(text)
    if not header or header.group(1) != expected_node or header.group(2) != expected_node:
        found = header.group(1) if header else "missing"
        raise PublishError(
            f"AUTO.md attribution mismatch: expected={expected_node} found={found} source={label}"
        )
    first = re.search(r"(?m)^### ", text)
    prefix = text if first is None else text[: first.start()]
    raw_blocks = [] if first is None else re.split(r"(?m)(?=^### )", text[first.start() :])
    blocks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in raw_blocks:
        if not raw.strip():
            continue
        keys = KEY_RE.findall(raw)
        if len(keys) != 1:
            raise PublishError(
                f"candidate block must contain exactly one stable key: node={expected_node}"
            )
        key = keys[0]
        if key in seen:
            raise PublishError(f"duplicate candidate key in source: node={expected_node} key={key}")
        if not STATUS_RE.search(raw) or not PIPELINE_RE.search(raw):
            raise PublishError(f"candidate metadata incomplete: node={expected_node} key={key}")
        seen.add(key)
        blocks.append((key, raw.rstrip("\n") + "\n"))
    return AutoDocument(prefix=prefix.rstrip("\n") + "\n", blocks=tuple(blocks))


def safe_target(worktree: Path, node: str) -> Path:
    root = worktree.resolve(strict=True)
    pages = root / "pages" / "nodes" / node
    parent = pages.parent.resolve(strict=True)
    if parent != (root / "pages" / "nodes").resolve(strict=True):
        raise PublishError(f"Wiki target parent escapes worktree: node={node}")
    if pages.exists() and pages.is_symlink():
        raise PublishError(f"Wiki node directory is a symlink: node={node}")
    if not pages.is_dir():
        raise PublishError(f"Wiki node directory is missing: node={node}")
    target = pages / "AUTO.md"
    if target.is_symlink():
        raise PublishError(f"Wiki AUTO.md target is a symlink: node={node}")
    return target


def plan_node(worktree: Path, spec: SourceSpec, source_text: str | None) -> PlanRow:
    if source_text is None:
        return PlanRow(spec.node, 0, 0, 0, 0, None, None, declared_empty=True)
    source = split_document(source_text, spec.node, spec.kind)
    target = safe_target(worktree, spec.node)
    if target.exists():
        try:
            existing_text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PublishError(f"cannot read Wiki target: node={spec.node}: {exc}") from exc
        existing = split_document(existing_text, spec.node, "wiki-target")
        existing_keys = {key for key, _ in existing.blocks}
        base = existing_text.rstrip("\n") + "\n"
    else:
        existing = AutoDocument(source.prefix, ())
        existing_keys = set()
        base = source.prefix

    unseen = [(key, block) for key, block in source.blocks if key not in existing_keys]
    content = base + "".join(block for _, block in unseen) if unseen else None
    return PlanRow(
        node=spec.node,
        source_count=len(source.blocks),
        existing_count=len(existing.blocks),
        appended=len(unseen),
        skipped_existing=len(source.blocks) - len(unseen),
        target=target,
        content=content,
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def wiki_worktree(args: argparse.Namespace) -> Path:
    if args.wiki_worktree:
        path = Path(args.wiki_worktree).expanduser()
    else:
        try:
            result = subprocess.run(
                [args.wiki_agent_bin, "write-path"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublishError(f"wiki-agent write-path failed: {exc}") from exc
        if result.returncode != 0 or not result.stdout.strip():
            raise PublishError(f"wiki-agent write-path failed rc={result.returncode}")
        path = Path(result.stdout.strip().splitlines()[-1])
    if not path.is_dir() or not (path / ".git").exists():
        raise PublishError(f"Wiki worktree is not a Git worktree: {path}")
    return path.resolve()


def submit_pr(args: argparse.Namespace, worktree: Path) -> None:
    if args.wiki_worktree:
        result = subprocess.run(
            [args.wiki_agent_bin, "write-path"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise PublishError(f"wiki-agent write-path verification failed rc={result.returncode}")
        owned = Path(result.stdout.strip().splitlines()[-1]).resolve()
        if owned != worktree:
            raise PublishError("--submit worktree differs from wiki-agent write-path")
    result = subprocess.run([args.wiki_agent_bin, "pr"], timeout=300, check=False)
    if result.returncode != 0:
        raise PublishError(f"wiki-agent pr failed rc={result.returncode}")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--local", action="append", default=[], metavar="NODE=PATH")
    ap.add_argument("--remote", action="append", default=[], metavar="NODE=SSH_HOST")
    ap.add_argument("--empty", action="append", default=[], metavar="NODE")
    ap.add_argument("--wiki-worktree", default=None)
    ap.add_argument("--wiki-agent-bin", default="wiki-agent")
    ap.add_argument("--ssh-bin", default="ssh")
    ap.add_argument("--ssh-timeout", type=int, default=30)
    ap.add_argument("--apply", action="store_true", help="write the fully validated plan")
    ap.add_argument("--submit", action="store_true", help="invoke one wiki-agent pr after apply")
    ap.add_argument("--json", action="store_true")
    return ap


def run(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.submit and not args.apply:
        raise PublishError("--submit requires --apply")
    if args.ssh_timeout <= 0:
        raise PublishError("--ssh-timeout must be positive")

    specs = build_specs(args)
    worktree = wiki_worktree(args)
    sources = {node: read_source(specs[node], args.ssh_bin, args.ssh_timeout) for node in ROSTER}
    # Build every row before the first write: one bad node leaves the Wiki
    # worktree untouched rather than partially staging a fleet batch.
    rows = [plan_node(worktree, specs[node], sources[node]) for node in ROSTER]
    changed = [row for row in rows if row.content is not None]
    if args.apply:
        for row in changed:
            assert row.target is not None and row.content is not None
            atomic_write(row.target, row.content)
    if args.submit and changed:
        submit_pr(args, worktree)

    report = {
        "schema": "ccc.auto-distill.wiki-publish-plan.v1",
        "mode": "applied" if args.apply else "preview",
        "roster": len(rows),
        "declared_empty": sum(row.declared_empty for row in rows),
        "source_candidates": sum(row.source_count for row in rows),
        "appended": sum(row.appended for row in rows),
        "skipped_existing": sum(row.skipped_existing for row in rows),
        "changed_pages": len(changed),
        "submitted": bool(args.submit and changed),
        "nodes": [
            {
                "node": row.node,
                "declared_empty": row.declared_empty,
                "source_candidates": row.source_count,
                "existing_candidates": row.existing_count,
                "appended": row.appended,
                "skipped_existing": row.skipped_existing,
            }
            for row in rows
        ],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "auto-distill Wiki batch: mode=%s roster=%d empty=%d source=%d "
            "append=%d existing=%d pages=%d submitted=%s"
            % (
                report["mode"],
                report["roster"],
                report["declared_empty"],
                report["source_candidates"],
                report["appended"],
                report["skipped_existing"],
                report["changed_pages"],
                str(report["submitted"]).lower(),
            )
        )
        for row in rows:
            state = "empty" if row.declared_empty else "ready"
            print(
                "  %-9s %-5s source=%d existing=%d append=%d skip=%d"
                % (
                    row.node,
                    state,
                    row.source_count,
                    row.existing_count,
                    row.appended,
                    row.skipped_existing,
                )
            )
    return 0


def main() -> int:
    try:
        return run()
    except PublishError as exc:
        print(f"publish_wiki: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
