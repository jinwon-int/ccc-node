#!/usr/bin/env python3
"""Agent trust-surface collector: planted instruction files + config drift (#2538).

Why this exists
---------------
forever.security (2026-10-22, "Hijacking AI agents via the Downloads folder")
showed that a web page can silently drop an ``AGENTS.md`` / ``CLAUDE.md`` into
``~/Downloads``; a coding agent later started in that folder treats the file as
instructions. The reproduced payloads rewrote machine-wide agent config: a new
``[mcp_servers.*]`` block in ``~/.codex/config.toml`` (installing the malicious
npm package ``function-color``) and ``ANTHROPIC_BASE_URL`` in
``.claude/settings.json`` pointing at an attacker host (OAuth token leak).

The fleet has a sharper variant of the same shape: A2A analysis sessions run
with cwd under ``/tmp/a2a-analysis-*`` and Claude Code loads ``CLAUDE.md`` from
every ancestor directory, so a single ``/tmp/CLAUDE.md`` in a world-writable
``/tmp`` would be injected into every worker session on that node.

TM-1306 (2026-07-18) deliberately removed blocking PreToolUse guards and
``permissions.deny``. This module is therefore **detection only**: it reads
metadata, never blocks, never modifies anything, and never prints secret
values (URLs are reduced to ``scheme://host``; env values other than endpoint
hosts are never emitted).

Consumers
---------
* ``scripts/ccc-security-audit.sh`` — maps findings to 위험/경고 rows.
* ``scripts/agent-trust-audit-fleet.sh`` — pipes this file to every node over
  ssh (``python3 - --json``) so the check does not depend on the remote
  checkout's age, then diffs ``signature`` against an accepted baseline.

Severity
--------
``danger``  IOC-class: an agent-instruction/config file planted in a download
            directory or directly in a shared ancestor root (``/``, ``/tmp``,
            ``/var/tmp``, ``$PREFIX/tmp``, ``/home``), or a known-bad package.
``warn``    Needs review: endpoint override to a non-allowlisted host,
            unpinned ``npx -y`` MCP server, unmanaged instruction file in a
            home directory (loaded by every bridge session), an agent session
            whose cwd was a download directory.
``info``    Inventory only.

Stdlib only, Python >= 3.8 (``tomllib`` used when available, regex fallback).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = "ccc.agent-trust.v1"

INSTRUCTION_NAMES = (
    "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md",
    "GEMINI.md", ".cursorrules",
)
# Paths (relative to a scanned directory) that configure an agent when it runs there.
CONFIG_RELPATHS = (
    ".mcp.json", ".claude/settings.json", ".claude/settings.local.json",
    ".codex/config.toml",
)
DOWNLOAD_DIRNAMES = ("Downloads", "downloads", "Download", "download", "storage/downloads")
ANDROID_DOWNLOAD_DIRS = ("/sdcard/Download", "/storage/emulated/0/Download")
KNOWN_BAD_NPM = ("function-color",)
DEFAULT_ENDPOINT_ALLOW = (
    "api.anthropic.com", "api.openai.com", "chatgpt.com", "chat.openai.com",
)
# Marker written by ccc-node's own memory loaders into home-level instruction files.
MANAGED_MARKERS = ("<!-- ccc-node:", "# ccc-node:")
ENDPOINT_KEY_RE = re.compile(r"(BASE_URL|_ENDPOINT|_PROXY)$|^HTTPS?_PROXY$", re.I)
DOWNLOAD_SCAN_DEPTH = 3
DOWNLOAD_SCAN_MAX_ENTRIES = 5000


def _env_list(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    return [x for x in re.split(r"[,\s]+", raw) if x]


def url_host(value):
    try:
        u = urlparse(str(value).strip())
    except Exception:
        return "<unparsable>"
    if not u.hostname:
        return "<non-url>"
    return f"{u.scheme}://{u.hostname}"


def _hostname(value):
    try:
        return (urlparse(str(value).strip()).hostname or "").lower()
    except Exception:
        return ""


def sha12(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except OSError:
        return "unreadable"


def in_git_worktree(path, stop):
    """True when ``path`` sits inside a git worktree rooted at/below ``stop``."""
    p = Path(path).parent
    stop = Path(stop)
    while True:
        if (p / ".git").exists():
            return True
        if p == stop or p.parent == p:
            return False
        p = p.parent


class Collector:
    def __init__(self, homes=None, tmp_roots=None, ancestor_roots=None,
                 npm_roots=None, endpoint_allow=None, fast=False):
        self.homes = homes if homes is not None else self._default_homes()
        self.tmp_roots = tmp_roots if tmp_roots is not None else self._default_tmp_roots()
        self.ancestor_roots = ancestor_roots if ancestor_roots is not None else self._default_ancestor_roots()
        self.npm_roots = npm_roots if npm_roots is not None else self._default_npm_roots()
        allow = endpoint_allow if endpoint_allow is not None else (
            list(DEFAULT_ENDPOINT_ALLOW) + _env_list("CCC_AGENT_TRUST_ENDPOINT_ALLOW", ()))
        self.endpoint_allow = {h.lower() for h in allow}
        self.fast = fast
        self.findings = []
        self.inventory = {"claude_mcp": [], "codex_mcp": [], "endpoints": [],
                          "home_instruction_files": [], "scanned": {}}

    # ---- defaults -------------------------------------------------------
    @staticmethod
    def _default_homes():
        env = os.environ.get("CCC_AGENT_TRUST_HOMES")
        if env is not None:
            return [h for h in env.split(":") if h]
        homes = []
        for h in [os.path.expanduser("~"), "/root"] + sorted(str(p) for p in Path("/home").glob("[!.]*")):
            if h and os.path.isdir(h) and os.access(h, os.R_OK | os.X_OK) and h not in homes:
                homes.append(h)
        return homes

    @staticmethod
    def _default_tmp_roots():
        env = os.environ.get("CCC_AGENT_TRUST_TMP_ROOTS")
        if env is not None:
            return [r for r in env.split(":") if r]
        roots = ["/tmp", "/var/tmp"]
        prefix = os.environ.get("PREFIX")
        if prefix:
            roots.append(os.path.join(prefix, "tmp"))
        tmpdir = os.environ.get("TMPDIR")
        if tmpdir:
            roots.append(tmpdir.rstrip("/") or "/")
        out = []
        for r in roots:
            if os.path.isdir(r) and r not in out:
                out.append(r)
        return out

    def _default_ancestor_roots(self):
        env = os.environ.get("CCC_AGENT_TRUST_ANCESTOR_ROOTS")
        if env is not None:
            return [r for r in env.split(":") if r]
        roots = ["/"]
        for h in self.homes:
            parent = os.path.dirname(h.rstrip("/"))
            if parent and parent not in roots:
                roots.append(parent)
        return roots

    def _default_npm_roots(self):
        env = os.environ.get("CCC_AGENT_TRUST_NPM_ROOTS")
        if env is not None:
            return [r for r in env.split(":") if r]
        roots = ["/usr/lib/node_modules", "/usr/local/lib/node_modules"]
        prefix = os.environ.get("PREFIX")
        if prefix:
            roots.append(os.path.join(prefix, "lib", "node_modules"))
        for h in self.homes:
            roots += [os.path.join(h, ".npm-global", "lib", "node_modules"),
                      os.path.join(h, ".local", "lib", "node_modules")]
        return [r for r in roots if os.path.isdir(r)]

    # ---- helpers --------------------------------------------------------
    def add(self, severity, kind, ident, detail=""):
        self.findings.append({"severity": severity, "kind": kind, "id": ident, "detail": detail})

    def _is_agent_file(self, rel):
        name = os.path.basename(rel)
        return name in INSTRUCTION_NAMES or any(rel.endswith(c) for c in CONFIG_RELPATHS)

    # ---- checks ---------------------------------------------------------
    def download_dirs(self):
        dirs = []
        for h in self.homes:
            for d in DOWNLOAD_DIRNAMES:
                p = os.path.join(h, d)
                if os.path.isdir(p):
                    rp = os.path.realpath(p)
                    if rp not in [os.path.realpath(x) for x in dirs]:
                        dirs.append(p)
        if os.environ.get("CCC_AGENT_TRUST_HOMES") is None:
            for d in ANDROID_DOWNLOAD_DIRS:
                if os.path.isdir(d) and os.access(d, os.R_OK | os.X_OK):
                    rp = os.path.realpath(d)
                    if rp not in [os.path.realpath(x) for x in dirs]:
                        dirs.append(d)
        return dirs

    def scan_downloads(self):
        dirs = self.download_dirs()
        self.inventory["scanned"]["download_dirs"] = dirs
        for top in dirs:
            seen = 0
            base_depth = top.rstrip("/").count("/")
            for root, subdirs, files in os.walk(top, followlinks=False):
                depth = root.rstrip("/").count("/") - base_depth
                seen += len(files) + len(subdirs)
                if seen > DOWNLOAD_SCAN_MAX_ENTRIES:
                    self.add("info", "download-scan-truncated", top, f"> {DOWNLOAD_SCAN_MAX_ENTRIES} entries")
                    break
                subdirs[:] = [s for s in subdirs if s not in ("node_modules", ".git")] if depth < DOWNLOAD_SCAN_DEPTH else []
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, top)
                    if not self._is_agent_file(rel):
                        continue
                    tracked = in_git_worktree(full, top)
                    sev = "warn" if tracked else "danger"
                    self.add(sev, "download-agent-file", full,
                             f"sha={sha12(full)} {'inside a git checkout' if tracked else 'outside git'}")

    def scan_ancestor_roots(self):
        roots = []
        for r in list(self.tmp_roots) + list(self.ancestor_roots):
            if r not in roots and os.path.isdir(r):
                roots.append(r)
        self.inventory["scanned"]["ancestor_roots"] = roots
        for r in roots:
            # Instruction files are read from every ancestor of the cwd, so one
            # here reaches every session below the root (danger). Config files
            # only apply when the cwd is exactly this root (warn).
            for name in INSTRUCTION_NAMES:
                p = os.path.join(r, name)
                if os.path.isfile(p):
                    self.add("danger", "ancestor-agent-file", p,
                             f"sha={sha12(p)} loaded by every agent session below {r}")
            for rel in CONFIG_RELPATHS:
                p = os.path.join(r, rel)
                if os.path.isfile(p):
                    self.add("warn", "ancestor-agent-config", p,
                             f"sha={sha12(p)} applies to agent sessions started in {r}")

    def scan_home_instruction_files(self):
        for h in self.homes:
            for name in INSTRUCTION_NAMES + (".mcp.json",):
                p = os.path.join(h, name)
                if not os.path.isfile(p):
                    continue
                digest = sha12(p)
                self.inventory["home_instruction_files"].append(f"{p}#{digest}")
                try:
                    head = Path(p).read_text(encoding="utf-8", errors="replace")[:4096]
                except OSError:
                    head = ""
                if any(m in head for m in MANAGED_MARKERS):
                    continue
                self.add("warn", "home-agent-file", p,
                         f"sha={digest} unmanaged, loaded by sessions whose cwd is {h}")

    def _check_endpoint(self, where, key, value):
        entry = f"{where}:{key}={url_host(value)}"
        self.inventory["endpoints"].append(entry)
        if _hostname(value) not in self.endpoint_allow:
            self.add("warn", "endpoint-override", entry,
                     "host not in allowlist (CCC_AGENT_TRUST_ENDPOINT_ALLOW)")

    def _check_mcp(self, lane, where, name, conf):
        if not isinstance(conf, dict):
            return
        if conf.get("url"):
            target = url_host(conf["url"])
        else:
            target = os.path.basename(str(conf.get("command", "")))
        self.inventory[f"{lane}_mcp"].append(f"{name} -> {target}")
        args = [str(a) for a in (conf.get("args") or [])]
        for bad in KNOWN_BAD_NPM:
            if any(re.search(rf"(^|[/\s]){re.escape(bad)}(@|$|\s)", a) for a in args):
                self.add("danger", "known-bad-npm", f"{lane}:{where}:{name}",
                         f"MCP server launches {bad} (forever.security 2026-10-22)")
        if os.path.basename(str(conf.get("command", ""))) in ("npx", "bunx", "pnpx"):
            pkgs = [a for a in args if not a.startswith("-")]
            pkg = pkgs[0] if pkgs else ""
            # Pinned = name@<version> (scoped names start with '@').
            body = pkg[1:] if pkg.startswith("@") else pkg
            version = body.split("@", 1)[1] if "@" in body else ""
            if not version or version in ("latest", "next"):
                self.add("warn", "unpinned-npx-mcp", f"{lane}:{where}:{name}",
                         f"{pkg or '?'} resolves the newest npm release each session")

    def _scan_claude_file(self, home, f):
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            self.add("info", "unparsable-config", f, "")
            return
        if not isinstance(data, dict):
            return
        short = f.replace(home, "~", 1)
        env = data.get("env")
        if isinstance(env, dict):
            for k, v in env.items():
                if ENDPOINT_KEY_RE.search(str(k)):
                    self._check_endpoint(f"claude:{short}", k, v)
        if data.get("apiKeyHelper"):
            self.inventory["endpoints"].append(f"claude:{short}:apiKeyHelper=present")
        for n, c in (data.get("mcpServers") or {}).items():
            self._check_mcp("claude", short, n, c)
        projects = data.get("projects")
        if isinstance(projects, dict):
            for ppath, pconf in projects.items():
                if isinstance(pconf, dict):
                    for n, c in (pconf.get("mcpServers") or {}).items():
                        self._check_mcp("claude", f"{short}[{ppath}]", n, c)

    def scan_claude(self):
        for h in self.homes:
            for f in (os.path.join(h, ".claude", "settings.json"),
                      os.path.join(h, ".claude", "settings.local.json"),
                      os.path.join(h, ".claude.json")):
                if os.path.isfile(f):
                    self._scan_claude_file(h, f)
            proj = os.path.join(h, ".claude", "projects")
            if os.path.isdir(proj):
                for d in os.listdir(proj):
                    if re.search(r"-(Downloads?|downloads?)(-|$)", d):
                        self.add("warn", "session-in-downloads", os.path.join(proj, d),
                                 "a Claude session ran with cwd in a download directory")

    def _load_toml(self, path):
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        try:
            import tomllib  # type: ignore
            return tomllib.loads(text)
        except ModuleNotFoundError:
            pass
        except Exception:
            return None
        # Minimal fallback: table headers + simple key = "string" pairs.
        data, cur = {}, None
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"^\[([^\]]+)\]$", line)
            if m:
                cur = data
                for part in m.group(1).split("."):
                    cur = cur.setdefault(part.strip().strip('"'), {})
                continue
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*"([^"]*)"', line)
            if m:
                (cur if cur is not None else data)[m.group(1)] = m.group(2)
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*=\s*\[(.*)\]", line)
            if m:
                (cur if cur is not None else data)[m.group(1)] = re.findall(r'"([^"]*)"', m.group(2))
        return data

    def scan_codex(self):
        for h in self.homes:
            f = os.path.join(h, ".codex", "config.toml")
            if not os.path.isfile(f):
                continue
            data = self._load_toml(f)
            short = f.replace(h, "~", 1)
            if not isinstance(data, dict):
                self.add("info", "unparsable-config", f, "")
                continue
            for key in ("chatgpt_base_url", "openai_base_url", "base_url"):
                if data.get(key):
                    self._check_endpoint(f"codex:{short}", key, data[key])
            for pname, pconf in (data.get("model_providers") or {}).items():
                if isinstance(pconf, dict) and pconf.get("base_url"):
                    self._check_endpoint(f"codex:{short}", f"model_providers.{pname}.base_url", pconf["base_url"])
            for n, c in (data.get("mcp_servers") or {}).items():
                self._check_mcp("codex", short, n, c)
            if not self.fast:
                self._scan_codex_sessions(h)

    def _scan_codex_sessions(self, home):
        sess = os.path.join(home, ".codex", "sessions")
        if not os.path.isdir(sess):
            return
        cutoff = time.time() - 30 * 86400
        hits = 0
        for root, _dirs, files in os.walk(sess):
            for f in files:
                if not f.endswith(".jsonl"):
                    continue
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        continue
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        first = fh.readline(65536)
                except OSError:
                    continue
                if re.search(r'"cwd"\s*:\s*"[^"]*/[Dd]ownloads?(/|")', first):
                    hits += 1
                    if hits <= 5:
                        self.add("warn", "session-in-downloads", p,
                                 "a Codex session ran with cwd in a download directory")

    def scan_npm(self):
        for r in self.npm_roots:
            for bad in KNOWN_BAD_NPM:
                p = os.path.join(r, bad)
                if os.path.exists(p):
                    self.add("danger", "known-bad-npm", p, "package named by forever.security 2026-10-22")

    def collect(self):
        self.scan_downloads()
        self.scan_ancestor_roots()
        self.scan_home_instruction_files()
        self.scan_claude()
        self.scan_codex()
        self.scan_npm()
        for k in ("claude_mcp", "codex_mcp", "endpoints", "home_instruction_files"):
            self.inventory[k] = sorted(set(self.inventory[k]))
        self.inventory["scanned"]["homes"] = self.homes
        return self.report()

    def report(self):
        sig = sorted(set(
            [f"endpoint\t{e}" for e in self.inventory["endpoints"]]
            + [f"claude-mcp\t{m}" for m in self.inventory["claude_mcp"]]
            + [f"codex-mcp\t{m}" for m in self.inventory["codex_mcp"]]
            + [f"home-file\t{x}" for x in self.inventory["home_instruction_files"]]
            + [f"finding\t{f['severity']} {f['kind']} {f['id']}" for f in self.findings
               if f["severity"] in ("danger", "warn")]
        ))
        counts = {s: sum(1 for f in self.findings if f["severity"] == s) for s in ("danger", "warn", "info")}
        return {
            "schema": SCHEMA,
            "host": socket.gethostname(),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "counts": counts,
            "findings": self.findings,
            "inventory": self.inventory,
            "signature": sig,
        }


def collect(**kw):
    return Collector(**kw).collect()


def render_human(rep):
    lines = [f"agent-trust {rep['host']}: danger={rep['counts']['danger']} "
             f"warn={rep['counts']['warn']} info={rep['counts']['info']}"]
    for f in rep["findings"]:
        lines.append(f"  [{f['severity']}] {f['kind']}: {f['id']}" + (f" ({f['detail']})" if f["detail"] else ""))
    inv = rep["inventory"]
    lines.append(f"  inventory: claude_mcp={len(inv['claude_mcp'])} codex_mcp={len(inv['codex_mcp'])} "
                 f"endpoints={len(inv['endpoints'])} home_files={len(inv['home_instruction_files'])}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only agent trust-surface collector (#2538).")
    ap.add_argument("--json", action="store_true", help="emit the ccc.agent-trust.v1 JSON report")
    ap.add_argument("--fast", action="store_true", help="skip Codex session history scan")
    args = ap.parse_args(argv)
    try:
        rep = collect(fast=args.fast)
    except Exception as exc:  # fail-soft: a collector crash must still be machine-readable
        rep = {"schema": SCHEMA, "host": socket.gethostname(), "error": type(exc).__name__}
        print(json.dumps(rep))
        return 0
    print(json.dumps(rep, ensure_ascii=False, sort_keys=True) if args.json else render_human(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
