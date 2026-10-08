#!/usr/bin/env python3
"""Pre-screen undecided autosave drafts with the fleet intake reviewer (#2183).

The human gate (`/skillsuggest`, #2011-B) keeps every draft until someone
reads it; by 2026-10-08 the fleet queue held ~603 drafts with ~34 arriving
per day, far beyond what a person reviews by hand. This module runs the same
independent reviewer the fleet-skills intake uses
(`scripts/skills-intake-review-handler.sh`, rubric 2026-08-28.2, areas A-H)
over each undecided draft BEFORE a human sees it, and records the verdict
next to the draft as `prescreen.json`:

- `reject` with at least one `blocker` finding → the draft is renamed into
  `skill-autosave-archive/prescreen-reject-<date>/` with a `manifest.jsonl`
  row (same root and shape as the 90-day expiry, #2184; `pending_expire.py
  restore <name>` brings it back). It leaves the `/skillsuggest` list.
- anything else (`approve`, `revise`, `reject` without a blocker) → the draft
  stays; `/skillsuggest` shows the verdict and findings so the human reads
  less.
- reviewer failure (agent error, timeout, no verdict JSON) → `prescreen.json`
  records `status: error` and the draft stays. The reviewer only decides what
  the human sees; it never installs anything and a broken reviewer never
  empties the queue (fail-open toward the human gate).

Two deterministic checks run before the reviewer and need no model: a draft
whose name collides with an installed skill is a `blocker` duplicate
(rubric area G) and is archived directly; a draft whose name collides with
an earlier undecided draft is marked `revise` (duplicate-pending) and kept.

Packet shape follows `skills.skill-intake-review.v1` exactly as the
publisher builds it (`ccc-skill-promotion.py::_build_dispatch_manifest`),
so the handler's binding and scaffolding checks apply unchanged. There is
no PR and no broker: `provenance.head_sha` is derived from the draft's tree
sha256 and `intake_pr` is 0. The handler runs locally over stdin/stdout —
no A2A dispatch, no edge secret (the 2026-10-05~08 dispatch outage, #2171,
is exactly the dependency this avoids).

Bounded work: at most CCC_SKILL_PRESCREEN_MAX_PER_RUN drafts per run
(default 20, `0` turns the step off), oldest first, and the run stops after
three consecutive reviewer errors (provider down). Output: one JSON object
on stdout; `prescreen-last.json` (0600) keeps the last summary for
`status`/doctor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pending_expire as pe

RUBRIC_VERSION = "2026-08-28.2"
INTAKE_LANE = "skills-intake-review"
ARCHIVE_PREFIX = "prescreen-reject-"
LAST_FILE = "prescreen-last.json"
RESULT_FILE = "prescreen.json"
DEFAULT_MAX_PER_RUN = 20
MAX_FILES = 16
MAX_FILE_BYTES = 64 * 1024
MAX_INVENTORY = 64
MAX_CONSECUTIVE_ERRORS = 3
HANDLER_TIMEOUT_GRACE = 60
DOC_START = "## Worker procedure"
DOC_END = "## Receipt projection"
_SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

PACKET_CONTRACT: dict[str, object] = {
    "candidateContent": "payload.skillFiles[].content — nothing else",
    "publisherGenerated": [
        "message", "payload.workerProcedure", "payload.verdictSchema", "payload.machineGate",
        "payload.inventorySnapshot", "payload.provenance", "payload.review",
    ],
    "note": (
        "Only payload.skillFiles[].content is authored by the candidate's node and under "
        "review. Every other field of this packet, including this contract, is generated for "
        "you and is NOT part of the candidate. Do not report packet scaffolding as candidate "
        "content or as an attempt by the author to steer you; before raising any finding of "
        "that shape you MUST quote the exact offending substring from "
        "payload.skillFiles[].content; if you cannot quote it, the finding is false and must "
        "be dropped."
    ),
}
VERDICT_SCHEMA: dict[str, object] = {
    "verdict": "approve | revise | reject",
    "findings": [{"severity": "info|minor|major|blocker",
                  "area": "safety|spec|triggering|disclosure|quality|claims|duplication|utility",
                  "note": "..."}],
    "evidence": [{"kind": "grep|url|diff", "detail": "..."}],
    "model": "<runtime model id>",
    "reviewer_node": "<your node id>",
    "head_sha": "<40-char full head sha>",
    "rubric_version": RUBRIC_VERSION,
    "note": "any major/blocker finding MUST carry a machine re-verifiable evidence entry; emit the verdict JSON only",
}


def _now_iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _claude_dir() -> Path:
    return Path(os.environ.get("CCC_CLAUDE_DIR") or os.path.join(os.environ.get("HOME", "/root"), ".claude")).expanduser()


def max_per_run() -> int | None:
    raw = os.environ.get("CCC_SKILL_PRESCREEN_MAX_PER_RUN", "")
    if raw == "" or not raw.isdigit():
        return DEFAULT_MAX_PER_RUN
    value = int(raw)
    return None if value == 0 else min(value, 500)


def resolve_handler() -> Path | None:
    """The reviewer handler to run. An explicit CCC_SKILL_PRESCREEN_HANDLER is
    authoritative — never fall back past it, so a test fixture or a deliberate
    override can never reach the real handler (and the real model) by accident."""
    explicit = os.environ.get("CCC_SKILL_PRESCREEN_HANDLER")
    if explicit:
        cand = Path(explicit)
        return cand if cand.is_file() and os.access(cand, os.X_OK) else None
    for cand in (
        Path("/usr/local/sbin/skills-intake-review-handler.sh"),
        _claude_dir() / "hooks" / "skills-intake-review-handler.sh",
        Path(__file__).resolve().parents[3] / "scripts" / "skills-intake-review-handler.sh",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def load_procedure() -> str | None:
    explicit = os.environ.get("CCC_SKILL_PRESCREEN_NEXUS_DIR")
    roots = [Path(explicit).expanduser()] if explicit else []
    home = Path(os.environ.get("HOME", "/root"))
    roots += [home / "work" / "a2a" / "a2a-nexus", home / "a2a-nexus", Path("/opt/a2a-nexus")]
    for root in roots:
        doc = root / "docs" / "skills-intake-review.md"
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            continue
        start, end = text.find(DOC_START), text.find(DOC_END)
        if start < 0 or end <= start:
            continue
        procedure = text[start:end].strip()
        if len(procedure) >= 256:
            return procedure
    return None


def _frontmatter(text: str) -> tuple[str, str]:
    name = re.search(r"^name:\s*(.+)$", text, re.M)
    desc = re.search(r"^description:\s*(.+)$", text, re.M)
    return (name.group(1).strip().strip("'\"") if name else "", desc.group(1).strip().strip("'\"")[:200] if desc else "")


def installed_inventory(skills_dir: Path) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not skills_dir.is_dir():
        return items
    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        skill = entry / "SKILL.md"
        if entry.is_symlink() or not entry.is_dir() or not skill.is_file():
            continue
        try:
            text = skill.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]
        except OSError:
            continue
        name, desc = _frontmatter(text)
        items.append({"name": name or entry.name, "audience": "local", "description": desc})
        if len(items) >= MAX_INVENTORY:
            break
    return items


def draft_files(entry: Path) -> tuple[list[dict[str, str]], str]:
    """Bounded, sorted candidate files + tree sha256 (same bounds as the publisher)."""
    files: list[dict[str, str]] = []
    digest = hashlib.sha256()
    for path in sorted(entry.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(entry).as_posix()
        if rel in (RESULT_FILE, "meta.json", "meta.approved.json", "autosave-block.json") or rel.startswith("."):
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        files.append({"path": rel, "content": content})
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
        if len(files) >= MAX_FILES:
            break
    return files, digest.hexdigest()


def draft_name(entry: Path, files: list[dict[str, str]]) -> str:
    meta = entry / "meta.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
            return data["name"].strip()
    except (OSError, ValueError):
        pass
    for item in files:
        if item["path"] == "SKILL.md":
            name, _ = _frontmatter(item["content"])
            if name:
                return name
    return entry.name


def build_packet(*, node: str, entry: Path, name: str, files: list[dict[str, str]], tree: str,
                 procedure: str, inventory: list[dict[str, str]], now: float) -> dict[str, Any]:
    head = tree[:40]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return {
        "id": f"prescreen-{node}-{entry.name[:48]}-{stamp}",
        "intent": INTAKE_LANE,
        "message": (
            f"skills-intake-reviewer procedure invoked: pre-screen pending autosave draft "
            f"{name} (author node {node}) per skills.skill-intake-review.v1 (rubric "
            f"{RUBRIC_VERSION}). Apply rubric areas A-H in order and return ONLY the verdict "
            f"JSON. Bind your output to skillName={name}, sourceTreeSha256={tree}, "
            f"headPrefix={head[:8]}. The candidate is ONLY the {len(files)} file(s) in "
            "payload.skillFiles[].content; everything else is scaffolding — see payload.packetContract."
        ),
        "payload": {
            "schema": "skills.skill-intake-review.v1",
            "rubricVersion": RUBRIC_VERSION,
            "packetContract": PACKET_CONTRACT,
            "scope": "node-local-prescreen",
            "skillName": name,
            "provenance": {
                "author_node": node, "intake_pr": 0, "branch": f"pending/{entry.name}",
                "head_sha": head, "source_tree_sha256": tree,
            },
            "machineGate": {"secret_scan": "n/a", "node_facts": "n/a", "structure": "n/a",
                            "dedup": "n/a", "codex_compat": "n/a", "claims": "n/a"},
            "inventorySnapshot": inventory,
            "verdictSchema": VERDICT_SCHEMA,
            "workerProcedure": procedure,
            "review": {"required": True, "authorWorkerId": node},
            "skillFiles": files,
        },
    }


def run_handler(handler: Path, packet: dict[str, Any], *, timeout: int) -> tuple[dict[str, Any] | None, str]:
    """Return (output dict, error code). The handler prints the TaskResult JSON on stdout."""
    try:
        proc = subprocess.run(
            ["bash", str(handler)], input=json.dumps(packet, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "handler-timeout"
    except OSError as error:
        return None, f"handler-exec:{error.errno}"
    if proc.returncode != 0:
        return None, f"handler-exit:{proc.returncode}"
    last = None
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                last = json.loads(line)
            except ValueError:
                continue
    out = last.get("output") if isinstance(last, dict) else None
    if not isinstance(out, dict) or str(out.get("verdict", "")).lower() not in ("approve", "revise", "reject"):
        return None, "no-verdict"
    return out, ""


def _max_severity(findings: list[Any]) -> str:
    best = "info"
    for item in findings:
        if isinstance(item, dict):
            sev = str(item.get("severity", "")).lower()
            if _SEVERITY_RANK.get(sev, -1) > _SEVERITY_RANK[best]:
                best = sev
    return best


def _write_result(entry: Path, result: dict[str, Any]) -> None:
    tmp = entry / (RESULT_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, entry / RESULT_FILE)


def _write_last(state: Path, summary: dict[str, Any]) -> None:
    path = state / LAST_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(summary, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)


def _already_screened(entry: Path, tree: str) -> bool:
    try:
        data = json.loads((entry / RESULT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("tree_sha256") == tree and data.get("status") == "ok"


class _Run:
    """Per-run context for screening one draft at a time (keeps `run` flat)."""

    def __init__(self, state: Path, handler: Path, procedure: str, inventory: list[dict[str, str]],
                 *, now: float, node: str, timeout: int, summary: dict[str, Any]) -> None:
        self.state, self.handler, self.procedure, self.inventory = state, handler, procedure, inventory
        self.now, self.node, self.timeout, self.summary = now, node, timeout, summary
        self.installed_names = {item["name"] for item in inventory}
        self.seen_names: set[str] = set()
        self.consecutive_errors = 0

    def deterministic(self, name: str) -> dict[str, Any] | None:
        if name in self.installed_names:
            return {"verdict": "reject", "findings": [{"severity": "blocker", "area": "duplication",
                    "note": f"an installed skill named {name!r} already exists on this node"}], "evidence": []}
        if name in self.seen_names:
            return {"verdict": "revise", "findings": [{"severity": "major", "area": "duplication",
                    "note": f"an older undecided draft already uses the name {name!r}"}], "evidence": []}
        return None

    def screen(self, entry: Path, name: str, files: list[dict[str, str]], tree: str) -> bool:
        """Screen one draft; returns False when the run must stop (reviewer down)."""
        result: dict[str, Any] = {
            "ts": _now_iso(self.now), "node": self.node, "skill_name": name, "tree_sha256": tree,
            "rubric_version": RUBRIC_VERSION, "status": "ok", "source": "deterministic",
        }
        verdict = self.deterministic(name)
        if verdict is None:
            packet = build_packet(node=self.node, entry=entry, name=name, files=files, tree=tree,
                                  procedure=self.procedure, inventory=self.inventory, now=self.now)
            out, error = run_handler(self.handler, packet, timeout=self.timeout)
            if out is None:
                self.consecutive_errors += 1
                self.summary["errors"] += 1
                result.update({"status": "error", "error": error, "source": "reviewer"})
                _write_result(entry, result)
                return self.consecutive_errors < MAX_CONSECUTIVE_ERRORS
            self.consecutive_errors = 0
            findings = out.get("findings") if isinstance(out.get("findings"), list) else []
            verdict = {
                "verdict": str(out.get("verdict")).lower(), "findings": findings[:20],
                "evidence": (out.get("evidence") if isinstance(out.get("evidence"), list) else [])[:20],
                "review_agent": out.get("review_agent"), "review_model": out.get("review_model"),
                "reviewer_node": out.get("reviewer_node"), "source": "reviewer",
            }
        result.update(verdict)
        self.seen_names.add(name)
        result["max_severity"] = _max_severity(result.get("findings", []))
        self.summary["reviewed"] += 1
        _write_result(entry, result)
        if result["verdict"] == "reject" and result["max_severity"] == "blocker":
            _archive(self.state, entry, now=self.now, node=self.node, result=result, summary=self.summary)
        else:
            self.summary["kept"][result["verdict"]] = self.summary["kept"].get(result["verdict"], 0) + 1
        return True


def _archive(state: Path, entry: Path, *, now: float, node: str, result: dict[str, Any], summary: dict[str, Any]) -> bool:
    try:
        archive_dir = pe.prepare_archive(state, now, prefix=ARCHIVE_PREFIX)
    except OSError as error:
        summary["failed"].append({"name": entry.name, "code": "archive-unavailable", "error": str(error)})
        return False
    dest = archive_dir / entry.name
    if dest.exists() or dest.is_symlink():
        summary["failed"].append({"name": entry.name, "code": "archive-name-taken"})
        return False
    try:
        os.rename(entry, dest)
    except OSError as error:
        summary["failed"].append({"name": entry.name, "code": f"rename-failed:{error.errno}"})
        return False
    blockers = [str(f.get("note", ""))[:160] for f in result.get("findings", []) if isinstance(f, dict) and f.get("severity") == "blocker"]
    pe.append_manifest(archive_dir, {
        "ts": _now_iso(now), "node": node, "from": str(state / "pending-skills"), "name": entry.name,
        "skill_name": result.get("skill_name", ""), "tree_sha256": result.get("tree_sha256", ""),
        "verdict": result.get("verdict"), "blockers": blockers[:5],
        "reason": "prescreen reject with blocker finding (rubric " + RUBRIC_VERSION + ", #2183)",
    })
    summary["archived"].append(entry.name)
    return True


def _collect(pending: Path, summary: dict[str, Any]) -> list[tuple[float, Path]]:
    todo: list[tuple[float, Path]] = []
    for entry in sorted(pending.iterdir(), key=lambda p: p.name):
        summary["scanned"] += 1
        reason = pe.classify(entry)
        if reason is not None:
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
            continue
        ts = pe.staged_at(entry)
        todo.append((ts if ts is not None else 0.0, entry))
    todo.sort(key=lambda item: (item[0], item[1].name))
    return todo


def run(state: Path, *, dry_run: bool, now: float | None = None, node: str = "") -> dict[str, Any]:
    now = time.time() if now is None else now
    cap = max_per_run()
    pending = state / "pending-skills"
    summary: dict[str, Any] = {
        "ts": _now_iso(now), "node": node, "dry_run": dry_run, "max_per_run": cap, "scanned": 0,
        "reviewed": 0, "archived": [], "kept": {"approve": 0, "revise": 0, "reject": 0},
        "errors": 0, "failed": [], "skipped": {}, "deferred": 0, "handler": None, "status": "clean",
    }
    if cap is None:
        summary["status"] = "off"
        return summary
    if pending.is_symlink() or not pending.is_dir():
        summary["status"] = "no-queue"
        return summary
    handler = resolve_handler()
    procedure = load_procedure()
    summary["handler"] = str(handler) if handler else None
    if handler is None or procedure is None:
        summary["status"] = "handler-unavailable" if handler is None else "procedure-unavailable"
        _write_last(state, summary)
        return summary
    skills_dir = Path(os.environ.get("CLAUDE_SKILLS_DIR") or (_claude_dir() / "skills"))
    inventory = installed_inventory(skills_dir)
    try:
        timeout = int(os.environ.get("REVIEW_TIMEOUT_SEC", "480")) + HANDLER_TIMEOUT_GRACE
    except ValueError:
        timeout = 480 + HANDLER_TIMEOUT_GRACE
    ctx = _Run(state, handler, procedure, inventory, now=now, node=node, timeout=timeout, summary=summary)
    work: list[tuple[Path, str, list[dict[str, str]], str]] = []
    for _, entry in _collect(pending, summary):
        files, tree = draft_files(entry)
        if not files:
            summary["skipped"]["no-files"] = summary["skipped"].get("no-files", 0) + 1
            continue
        name = draft_name(entry, files)
        if _already_screened(entry, tree):
            summary["skipped"]["already-screened"] = summary["skipped"].get("already-screened", 0) + 1
            ctx.seen_names.add(name)  # an older, already-screened draft owns this name
            continue
        work.append((entry, name, files, tree))
    summary["deferred"] = max(0, len(work) - cap)
    work = work[:cap]
    if dry_run:
        summary["status"] = "dry-run"
        summary["would_review"] = [{"name": n, "dir": e.name} for e, n, _, _ in work]
        return summary
    for entry, name, files, tree in work:
        if not ctx.screen(entry, name, files, tree):
            summary["status"] = "reviewer-down"
            break
    if summary["status"] == "clean" and (summary["reviewed"] or summary["errors"]):
        summary["status"] = "reviewed"
    _write_last(state, summary)
    return summary


def status(state: Path) -> dict[str, Any]:
    try:
        last = json.loads((state / LAST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    pending = state / "pending-skills"
    screened = {"approve": 0, "revise": 0, "reject": 0, "error": 0, "unscreened": 0}
    if pending.is_dir() and not pending.is_symlink():
        for entry in pending.iterdir():
            if pe.classify(entry) is not None:
                continue
            try:
                data = json.loads((entry / RESULT_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                screened["unscreened"] += 1
                continue
            key = "error" if data.get("status") != "ok" else str(data.get("verdict", "error"))
            screened[key if key in screened else "error"] += 1
    return {"last": last, "queue": screened, "max_per_run": max_per_run()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="pre-screen undecided drafts with the intake reviewer")
    run_p.add_argument("--dry-run", action="store_true", help="list what would be reviewed; call nothing")
    run_p.add_argument("--now", type=float, default=None)
    sub.add_parser("status", help="last-run summary and per-draft verdict counts (read-only)")
    args = parser.parse_args(argv)
    state = pe._state_dir()
    node = os.environ.get("CCC_NODE", "") or os.uname().nodename.split(".")[0]
    out = run(state, dry_run=args.dry_run, now=args.now, node=node) if args.cmd == "run" else status(state)
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
