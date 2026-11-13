#!/usr/bin/env python3
"""Local static code graph for jinwon-int Python repos (ccc-node#2707 stage 1).

stdlib ``ast`` only. ``build`` writes one JSONL per commit; the queries
(``callers``/``callees``/``imports-of``/``impact``) read it, building on
demand under the same gates. ``calls`` edges are name-based best guesses:
``exact`` only when lexical scope or an explicit import resolves to exactly
one definition in the graph; everything else (attribute calls on objects,
``self.x()``, globally ambiguous names, external modules) is ``name-match``.
Exit codes: 0 ok, 1 error, 2 usage, 3 refused by policy.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

SCHEMA = 1
CONFIG_NAME = ".ccc-code-graph.toml"
ALLOWED_OWNER = "jinwon-int"
MAX_FILE_BYTES = 2 * 1024 * 1024
KEEP_PER_REPO = 8
BUILTINS = frozenset(dir(builtins))
ORIGIN_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class Refused(Exception):
    """Policy refusal (exit 3): not opted in, foreign origin, dirty tree."""


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         errors="replace")
    if out.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def glob_re(pattern: str) -> re.Pattern[str]:
    parts, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        else:
            ch = pattern[i]
            parts.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def read_toml(path: Path, strict: bool = False) -> dict | None:
    """Parsed TOML; None when tomllib is missing (Python < 3.11) or, unless strict, unreadable."""
    try:
        import tomllib
    except ImportError:
        print(f"warning: tomllib unavailable; {path.name} ignored", file=sys.stderr)
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        if strict:
            raise Refused(f"{path.name} unreadable: {exc}") from None
        return None


def load_config(root: Path) -> tuple[list[str], list[str], dict]:
    """include/exclude globs and an optional ``packages = {import_name = "dir"}`` table."""
    cfg = {"include": ["**/*.py"], "exclude": [], "packages": {}}
    path = root / CONFIG_NAME
    data = read_toml(path, strict=True) if path.is_file() else {}
    for key in ("include", "exclude"):
        value = (data or {}).get(key, cfg[key])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise Refused(f"{CONFIG_NAME}: {key} must be a list of glob strings")
        cfg[key] = value
    packages = (data or {}).get("packages", {})
    if not isinstance(packages, dict) or not all(isinstance(v, str) for v in packages.values()):
        raise Refused(f"{CONFIG_NAME}: packages must map import names to directories")
    return cfg["include"], cfg["exclude"], packages


def package_map(root: Path, names: list[str], packages: dict) -> list[tuple[str, str]]:
    """(import prefix, module-id prefix) pairs from setuptools package-dir and the config."""
    pairs = dict(packages)
    for rel in (n for n in names if n.rsplit("/", 1)[-1] == "pyproject.toml"):
        data = read_toml(root / rel) or {}
        dirs = data.get("tool", {}).get("setuptools", {}).get("package-dir", {})
        base = rel.rpartition("/")[0]
        for imp, sub in (dirs.items() if isinstance(dirs, dict) else ()):
            if isinstance(sub, str):
                pairs.setdefault(imp, os.path.normpath(os.path.join(base, sub)))
    return [(imp, "" if d == "." else d.replace("/", ".")) for imp, d in sorted(pairs.items())]


def repo_identity(root: Path) -> tuple[str, str]:
    url = git(root, "remote", "get-url", "origin").strip()
    match = ORIGIN_RE.match(url)
    if not match or match["owner"].lower() != ALLOWED_OWNER:
        raise Refused(f"origin is not a github.com/{ALLOWED_OWNER}/* repo (external-github-content)")
    return match["owner"], match["repo"]


def dirty_marker(root: Path, worktree: bool) -> str | None:
    """None when the scanned file set matches HEAD; else a hash of the dirty content.

    Untracked files only count with --worktree, because only then are they scanned.
    """
    if not git(root, "status", "--porcelain", "--untracked-files=" + ("all" if worktree else "no")):
        return None
    digest = hashlib.sha256(git(root, "diff", "HEAD", "--binary").encode())
    others = git(root, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if worktree else []
    for rel in sorted(o for o in others if o.endswith(".py")):
        path = root / rel
        digest.update(rel.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() and not path.is_symlink() else b"<gone>")
    return digest.hexdigest()[:10]


def cache_root(arg: str | None) -> Path:
    if arg or os.environ.get("CCC_CODE_GRAPH_CACHE"):
        return Path(arg or os.environ["CCC_CODE_GRAPH_CACHE"]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ccc-code-graph"


def resolve_target(args: argparse.Namespace, path: str) -> dict:
    """Apply every gate and return the repo root, identity and cache file."""
    root = Path(git(Path(path).resolve(), "rev-parse", "--show-toplevel").strip())
    owner, repo = repo_identity(root)
    if not (root / CONFIG_NAME).is_file() and not args.repo:
        raise Refused(f"no {CONFIG_NAME} at repo root; pass --repo to opt in explicitly")
    sha = git(root, "rev-parse", "HEAD").strip()
    key = sha[:12]
    marker = dirty_marker(root, args.worktree)
    if marker and not args.worktree:
        raise Refused("worktree has uncommitted tracked changes; commit or pass --worktree")
    if marker:
        key += f"+dirty-{marker}"
    cache = cache_root(args.cache_root)
    return {"root": root, "slug": f"{owner}/{repo}", "sha": sha, "dirty": bool(marker),
            "file": cache / f"{owner}__{repo}@{key}.jsonl", "prefix": f"{owner}__{repo}@"}


def list_files(root: Path, worktree: bool) -> tuple[list[str], list[tuple[str, str]]]:
    extra = ["--others", "--exclude-standard"] if worktree else []
    names = git(root, "ls-files", "-z", "--cached", *extra).split("\0")
    include, exclude, packages = load_config(root)
    inc, exc = [glob_re(p) for p in include], [glob_re(p) for p in exclude]
    files = sorted({n for n in names if n.endswith(".py") and any(r.match(n) for r in inc)
                    and not any(r.match(n) for r in exc)})
    return files, package_map(root, names, packages)


def module_id(rel: str) -> tuple[str, bool]:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]) or "__init__", True
    return ".".join(parts), False


class Collector(ast.NodeVisitor):
    """Pass 1: definitions, imports and unresolved call sites of one module."""

    def __init__(self, mod: str, rel: str, is_pkg: bool):
        self.mod, self.rel, self.is_pkg = mod, rel, is_pkg
        self.nodes: list[dict] = []
        self.defines: list[dict] = []
        self.imports: list[tuple[str, str | None, str | None, int, bool]] = []  # base, name, alias, line, relative
        self.calls: list[tuple[str, tuple[str, ...], str, int]] = []  # src, chain, scope, line
        self.top: dict[str, str] = {}  # module-level def name -> id
        self.stack: list[dict] = []  # {id, kind, locals, nested}

    def at(self, node: ast.AST) -> str:
        return f"{self.rel}:{getattr(node, 'lineno', 1)}"

    def _define(self, node: ast.AST, name: str, kind: str) -> dict:
        parent = self.stack[-1]["id"] if self.stack else self.mod
        ident = f"{parent}.{name}"
        self.nodes.append({"kind": "node", "type": kind, "id": ident, "name": name, "at": self.at(node)})
        self.defines.append({"kind": "edge", "type": "defines", "src": parent, "dst": ident,
                             "at": self.at(node)})
        if not self.stack:
            self.top[name] = ident
        elif self.stack[-1]["kind"] == "function":
            self.stack[-1]["nested"][name] = ident
        return {"id": ident, "kind": kind, "locals": set(), "nested": {}}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in node.decorator_list + node.bases:
            self.visit(expr)
        self.stack.append(self._define(node, node.name, "class"))
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expr in node.decorator_list:
            self.visit(expr)
        frame = self._define(node, node.name, "function")
        a = node.args
        frame["locals"] = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
        frame["locals"] |= {x.arg for x in (a.vararg, a.kwarg) if x}
        frame["locals"] |= {n.id for n in walk_own(node) if isinstance(n, ast.Name)
                            and isinstance(n.ctx, ast.Store)}
        frame["self_class"] = self.stack[-1]["id"] if self.stack and self.stack[-1]["kind"] == "class" else None
        self.stack.append(frame)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append((alias.name, None, alias.asname, node.lineno, False))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            pkg = self.mod.split(".") if self.is_pkg else self.mod.split(".")[:-1]
            if node.level - 1 > len(pkg):
                return  # beyond the top-level package: unresolvable
            pkg = pkg[: len(pkg) - (node.level - 1)]
            base = ".".join(pkg + ([base] if base else []))
        for alias in node.names:
            if alias.name != "*":
                self.imports.append((base, alias.name, alias.asname, node.lineno, node.level > 0))

    def visit_Call(self, node: ast.Call) -> None:
        chain = dotted(node.func)
        if chain:
            self.calls.append((self.stack[-1]["id"] if self.stack else self.mod, chain,
                               self._scope(chain[0]), node.lineno))
        self.generic_visit(node)

    def _scope(self, root: str) -> str:
        for frame in reversed(self.stack):
            if frame["kind"] != "function":
                continue  # class bodies are not enclosing scopes for methods
            if root in frame["nested"]:
                return "nested:" + frame["nested"][root]
            if root in ("self", "cls") and frame.get("self_class"):
                return "self:" + frame["self_class"]
            if root in frame["locals"]:
                return "local"
        return "global"


def walk_own(func: ast.AST):
    """ast.walk limited to one function body (nested defs/classes excluded)."""
    todo = list(ast.iter_child_nodes(func))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(node))


def dotted(expr: ast.AST) -> tuple[str, ...] | None:
    names = []
    while isinstance(expr, ast.Attribute):
        names.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        return (expr.id, *reversed(names))
    return (names[0],) if names else None  # f().g() -> name-match on "g"


class Graph:
    """Pass 2: resolve imports to internal modules and calls to definitions."""

    def __init__(self, collectors: list[Collector], pkg_map: list[tuple[str, str]]):
        self.mods = {c.mod: c for c in collectors}
        self.pkg_map = pkg_map
        self.roots = {m.split(".")[0] for m in self.mods}
        self.defs = {n["id"]: n for c in collectors for n in c.nodes}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for ident, node in self.defs.items():
            self.by_name[node["name"]].append(ident)
        self.aliases: dict[str, dict[str, str]] = {}

    def find_module(self, name: str, importer: str, relative: bool = False) -> str | None:
        """Import name -> internal module id; absolute names also try the importer's ancestor dirs."""
        if relative or not name:
            return name if name in self.mods else None
        prefix = importer.split(".")[:-1] if not self.mods[importer].is_pkg else importer.split(".")
        for cut in range(len(prefix), -1, -1):
            cand = ".".join(prefix[:cut] + [name])
            if cand in self.mods:
                return cand
        for imp, target in self.pkg_map:  # installed-name -> source-dir mappings
            if not imp or name == imp or name.startswith(imp + "."):
                rest = name[len(imp):].lstrip(".") if imp else name
                cand = ".".join(p for p in (target, rest) if p)
                if cand in self.mods:
                    return cand
        return None

    def import_edges(self, c: Collector) -> list[dict]:
        edges, local = [], {}
        for base, name, alias, line, rel in c.imports:
            target_mod = self.find_module(base, c.mod, rel)
            if name is None:  # import a.b [as c]
                bound = alias or base.split(".")[0]
                local[bound] = (target_mod or base) if alias else (self.find_module(bound, c.mod) or bound)
                dst = target_mod or base
            else:
                sub = self.find_module(f"{base}.{name}" if base else name, c.mod, rel)
                dst = sub or target_mod or base
                local[alias or name] = sub or f"{target_mod or base}.{name}"
            edges.append({"kind": "edge", "type": "imports", "src": c.mod, "dst": dst,
                          "internal": dst in self.mods, "at": f"{c.rel}:{line}"})
        self.aliases[c.mod] = local
        return edges

    def follow(self, ident: str, depth: int = 0) -> str | None:
        """A dotted name -> definition id, following package re-exports."""
        if ident in self.defs:
            return ident
        head, _, last = ident.rpartition(".")
        if depth > 5 or not head:
            return None
        if head in self.mods:
            nxt = self.aliases.get(head, {}).get(last)
            return self.follow(nxt, depth + 1) if nxt and nxt != ident else None
        resolved = self.follow(head, depth + 1)
        return self.follow(f"{resolved}.{last}", depth + 1) if resolved else None

    def call_edge(self, c: Collector, src: str, chain: tuple[str, ...], scope: str, line: int) -> dict | None:
        name, rest = chain[0], chain[1:]
        edge = {"kind": "edge", "type": "calls", "src": src, "at": f"{c.rel}:{line}"}
        if scope.startswith("nested:") and not rest:
            return {**edge, "dst": scope[7:], "confidence": "exact"}
        if scope.startswith("self:") and len(rest) == 1 and f"{scope[5:]}.{rest[0]}" in self.defs:
            # best guess only: a subclass may override (dynamic dispatch is not resolved)
            return {**edge, "dst": f"{scope[5:]}.{rest[0]}", "confidence": "name-match"}
        if scope == "global":
            base = c.top.get(name) or self.aliases[c.mod].get(name)
            if base:
                full = ".".join((base, *rest))
                target = self.follow(full)
                if target:
                    return {**edge, "dst": target, "confidence": "exact"}
                if base.split(".")[0] not in self.roots:
                    return {**edge, "dst": full, "confidence": "name-match", "external": True}
            elif not rest and name in BUILTINS:
                return None
        candidates = len(self.by_name.get(chain[-1], ()))
        if not candidates:
            return None  # nothing in the graph carries that name (stdlib method, attribute...)
        return {**edge, "dst": chain[-1], "confidence": "name-match", "candidates": candidates}

    def records(self) -> list[dict]:
        out: list[dict] = []
        for c in self.mods.values():
            out.append({"kind": "node", "type": "module", "id": c.mod, "name": c.mod.rsplit(".", 1)[-1],
                        "at": f"{c.rel}:1"})
            out += c.nodes + c.defines + self.import_edges(c)
        for c in self.mods.values():
            out += [e for call in c.calls if (e := self.call_edge(c, *call))]
        return out


def build(args: argparse.Namespace) -> dict:
    t = resolve_target(args, args.path)
    if t["file"].is_file() and not args.force:
        return {**load_meta(t["file"]), "cached": True, "cache_file": str(t["file"])}
    started = time.monotonic()
    collectors, skipped = [], []
    files, pkg_map = list_files(t["root"], args.worktree)
    for rel in files:
        path = t["root"] / rel
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            skipped.append({"path": rel, "reason": "not-regular-or-too-large"})
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=rel)
        except (SyntaxError, ValueError, RecursionError, UnicodeDecodeError) as exc:
            skipped.append({"path": rel, "reason": f"syntax: {type(exc).__name__}"})
            continue
        mod, is_pkg = module_id(rel)
        collector = Collector(mod, rel, is_pkg)
        collector.visit(tree)
        collectors.append(collector)
    records = Graph(collectors, pkg_map).records()
    meta = {"kind": "meta", "schema": SCHEMA, "repo": t["slug"], "sha": t["sha"], "dirty": t["dirty"],
            "files": len(collectors), "skipped": skipped,
            "nodes": sum(r["kind"] == "node" for r in records),
            "edges": sum(r["kind"] == "edge" for r in records),
            "build_seconds": round(time.monotonic() - started, 3)}
    write_cache(t, [meta, *records])
    return {**meta, "cached": False, "cache_file": str(t["file"])}


def write_cache(t: dict, records: list[dict]) -> None:
    target: Path = t["file"]
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(f".tmp{os.getpid()}")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    os.replace(tmp, target)
    old = sorted(target.parent.glob(t["prefix"] + "*.jsonl"), key=lambda p: p.stat().st_mtime)
    for stale in old[:-KEEP_PER_REPO]:
        stale.unlink(missing_ok=True)


def load_meta(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.loads(fh.readline())


def load_graph(args: argparse.Namespace) -> list[dict]:
    info = build(args)
    if not info["cached"]:
        print(f"built graph: {info['cache_file']}", file=sys.stderr)
    with open(info["cache_file"], encoding="utf-8") as fh:
        return [json.loads(line) for line in fh][1:]


def matches(ident: str, symbol: str) -> bool:
    return ident == symbol or ident.endswith("." + symbol) or ident == symbol.rsplit(".", 1)[-1]


def as_module(target: str) -> str:
    return module_id(target)[0] if target.endswith(".py") else target


def query(args: argparse.Namespace) -> list[dict]:
    recs = load_graph(args)
    edges = [r for r in recs if r["kind"] == "edge"]
    if args.cmd == "callers":
        return [e for e in edges if e["type"] == "calls" and matches(e["dst"], args.symbol)]
    if args.cmd == "callees":
        return [e for e in edges if e["type"] == "calls" and matches(e["src"], args.symbol)]
    if args.cmd == "imports-of":
        mod, side = as_module(args.module), "dst" if args.reverse else "src"
        return [e for e in edges if e["type"] == "imports" and e[side] == mod]
    return impact(recs, args.files, args.depth)


def impact(recs: list[dict], files: list[str], depth: int) -> list[dict]:
    path_of = {r["id"]: r["at"].rsplit(":", 1)[0] for r in recs if r.get("type") == "module"}
    importers: dict[str, set[str]] = defaultdict(set)
    for r in recs:
        if r.get("type") == "imports" and r["internal"] and r["src"] != r["dst"]:
            importers[r["dst"]].add(r["src"])
    seen = {module_id(f.removeprefix("./"))[0] for f in files}
    frontier, out = sorted(seen), []
    for level in range(1, depth + 1):
        nxt = []
        for mod in frontier:
            for src in sorted(importers.get(mod, ())):
                if src not in seen:
                    seen.add(src)
                    nxt.append(src)
                    out.append({"depth": level, "path": path_of[src], "via": path_of.get(mod, mod)})
        frontier = nxt
    return out


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no results)")
        return
    cols = [k for k in ("depth", "confidence", "src", "dst", "internal", "path", "via", "at") if k in rows[0]]
    widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in cols}
    print("  ".join(k.upper().ljust(widths[k]) for k in cols))
    for r in rows:
        print("  ".join(str(r.get(k, "")).ljust(widths[k]) for k in cols).rstrip())


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", action="store_true", help=f"opt in without {CONFIG_NAME}")
    common.add_argument("--worktree", action="store_true", help="allow a dirty tree (dirty cache key)")
    common.add_argument("--cache-root", help="cache dir (default $CCC_CODE_GRAPH_CACHE or ~/.cache/ccc-code-graph)")
    common.add_argument("--json", action="store_true")
    common.add_argument("--force", action="store_true", help="rebuild even if cached")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", parents=[common]).add_argument("path")
    for cmd, arg in (("callers", "symbol"), ("callees", "symbol"), ("imports-of", "module")):
        sp = sub.add_parser(cmd, parents=[common])
        sp.add_argument(arg)
        sp.add_argument("--path", default=".", help="repo path (default: cwd)")
    sub.choices["imports-of"].add_argument("--reverse", action="store_true", help="importers instead")
    sp = sub.add_parser("impact", parents=[common])
    sp.add_argument("files", nargs="+", help="repo-relative .py paths")
    sp.add_argument("--depth", type=int, default=2)
    sp.add_argument("--path", default=".")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "build":
            info = build(args)
            if args.json:
                print(json.dumps(info, ensure_ascii=False))
            else:
                state = "cached" if info["cached"] else f"built in {info['build_seconds']}s"
                print(f"{info['repo']}@{info['sha'][:12]}: files={info['files']} nodes={info['nodes']} "
                      f"edges={info['edges']} skipped={len(info['skipped'])} ({state}) -> {info['cache_file']}")
                for s in info["skipped"]:
                    print(f"  skipped {s['path']}: {s['reason']}")
            return 0
        rows = query(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
