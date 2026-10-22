#!/usr/bin/env python3
"""Fleet skill retirement PR generator (#2239 L1, owner decision 2026-10-22).

    ccc-skill-retire.py plan --aggregate FILE [--fleet-skills-dir DIR] [--max N] [--exclude NAME]...
    ccc-skill-retire.py pr   --aggregate FILE [--yes] [--fleet-skills-dir DIR] [--max N] [--exclude NAME]...

Input is the fleet aggregate produced by ``ccc-skill-usage-report.py aggregate
--json``: its ``retire_candidates`` are fleet-managed skills never used on any
of enough nodes (usage facts only). This tool adds the age rule from the canon
repository: ``approved/<audience>/<name>/approval.json`` ``approved_at`` is
when distribution started, and a candidate qualifies only when at least
``--min-idle-days`` (default 60) have passed since then. A node's
``installed_at`` cannot anchor that rule — it is the fleet marker's mtime,
rewritten by every sync ref change.

``plan`` is read-only. It resolves ages from ``--fleet-skills-dir`` (an
existing clone) or, when absent, from a fresh read-only clone in a 0700
temporary directory. ``pr`` without ``--yes`` is the same dry run; with
``--yes`` it moves each selected ``approved/<audience>/<name>/`` to
``retired/<audience>/<name>/`` (``git mv``, history preserved), writes
``retirement.json`` beside it, commits on ``retire/auto-<date>-<8hex>``, pushes
and opens a non-draft pull request. The PR needs human approval (#1739 —
archive moves are PR-first with owner approval); this tool never approves,
merges or enqueues. Merging makes every node prune the skill on its next sync
(``ccc-fleet-skills-sync.py`` RETIRE_PRUNE, backup kept); restoring is a
reverse-move PR.

Output is one JSON object: ``{ok, mode, selected, skipped, pr_url?, branch?,
head?}``. Names and counters only — never SKILL.md bodies or descriptions.
Failures are closed-vocabulary codes (exit 2), never tracebacks with paths.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any

AGGREGATE_KIND = "ccc-skill-usage-aggregate"
AGGREGATE_SCHEMA = 1
RETIREMENT_SCHEMA = 1
DEFAULT_REPO = "jinwon-int/fleet-skills"
DEFAULT_BASE = "main"
DEFAULT_MIN_IDLE_DAYS = 60
DEFAULT_MAX = 50
MAX_AGGREGATE_BYTES = 16 * 1024 * 1024
MAX_APPROVAL_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT = 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# fleet-skills audiences (validate.py AUDIENCES; retired/ lane fleet-skills#565).
AUDIENCES = ("shared", "claude", "codex", "piri", "danso")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_NO_BACKGROUND = ("-c", "core.hooksPath=/dev/null", "-c", "credential.helper=")


class RetireError(RuntimeError):
    """Closed-vocabulary failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_regular(path: Path, *, max_bytes: int, code: str) -> bytes:
    """Bytes of a regular, non-symlink file under the cap; anything else fails closed."""
    try:
        meta = path.lstat()
    except OSError:
        raise RetireError(code) from None
    if not stat.S_ISREG(meta.st_mode) or meta.st_size > max_bytes:
        raise RetireError(code)
    try:
        return path.read_bytes()
    except OSError:
        raise RetireError(code) from None


def _load_aggregate(path: Path) -> tuple[dict[str, Any], str]:
    payload = _read_regular(path, max_bytes=MAX_AGGREGATE_BYTES, code="aggregate_unreadable")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RetireError("aggregate_not_json") from None
    if (
        not isinstance(value, dict)
        or value.get("kind") != AGGREGATE_KIND
        or value.get("schema_version") != AGGREGATE_SCHEMA
        or not isinstance(value.get("retire_candidates"), list)
        or not isinstance(value.get("nodes"), dict)
        or _parse_ts(value.get("generated_at")) is None
    ):
        raise RetireError("aggregate_invalid")
    return value, hashlib.sha256(payload).hexdigest()


def _candidates(aggregate: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in aggregate["retire_candidates"]:
        if not isinstance(item, dict):
            raise RetireError("aggregate_invalid")
        name = item.get("name")
        holders = item.get("holder_count")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in seen:
            raise RetireError("aggregate_invalid")
        if not isinstance(holders, int) or holders < 1:
            raise RetireError("aggregate_invalid")
        seen.add(name)
        rows.append({"name": name, "holders": holders})
    return sorted(rows, key=lambda row: row["name"])


# ---------------------------------------------------------------------------
# git / gh
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, cwd: Path | None = None, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess[bytes]:
    if argv and argv[0] == "git":
        argv = ["git", *_GIT_NO_BACKGROUND, *argv[1:]]
    try:
        completed = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RetireError("command_failed") from None
    if len(completed.stdout) > MAX_COMMAND_OUTPUT or len(completed.stderr) > MAX_COMMAND_OUTPUT:
        raise RetireError("command_output_too_large")
    if check and completed.returncode != 0:
        raise RetireError("command_failed")
    return completed


def _head_sha(repo_dir: Path) -> str:
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.decode("ascii", "replace").strip()
    if not SHA_RE.fullmatch(sha):
        raise RetireError("head_unresolved")
    return sha


def _private_tmpdir() -> Path:
    try:
        path = Path(tempfile.mkdtemp(prefix="ccc-skill-retire."))
        os.chmod(path, 0o700)
    except OSError:
        raise RetireError("tmpdir_failed") from None
    return path


def _clone(remote: str, base: str, into: Path) -> Path:
    repo_dir = into / "repo"
    _run(["git", "clone", "--quiet", "--branch", base, "--single-branch", remote, str(repo_dir)])
    return repo_dir


def _existing_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir() or not (path / ".git").exists():
        raise RetireError("fleet_skills_dir_invalid")
    return path


def _gh_identity() -> tuple[str, str]:
    """(user.name, user.email) for the retire commit, from the local gh account."""
    out = _run(["gh", "api", "user", "--jq", ".id,.login"]).stdout.decode("utf-8", "replace").split()
    if len(out) != 2 or not out[0].isdigit() or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", out[1]):
        raise RetireError("gh_identity_unavailable")
    return out[1], f"{out[0]}+{out[1]}@users.noreply.github.com"


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def _locate(repo_dir: Path, name: str) -> tuple[str, Path] | None:
    root = repo_dir / "approved"
    if not root.is_dir() or root.is_symlink():
        raise RetireError("approved_root_missing")
    for audience_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        if audience_dir.name not in AUDIENCES:
            continue
        skill_dir = audience_dir / name
        if skill_dir.is_dir() and not skill_dir.is_symlink():
            return audience_dir.name, skill_dir
    return None


def _approved_at(skill_dir: Path) -> datetime:
    payload = _read_regular(skill_dir / "approval.json", max_bytes=MAX_APPROVAL_BYTES, code="approval_unreadable")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RetireError("approval_invalid") from None
    when = _parse_ts(value.get("approved_at")) if isinstance(value, dict) else None
    if when is None:
        raise RetireError("approval_invalid")
    return when


def _select(
    repo_dir: Path,
    candidates: list[dict[str, Any]],
    *,
    now: datetime,
    min_idle_days: int,
    limit: int,
    exclude: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in candidates:
        name = row["name"]
        if name in exclude:
            skipped.append({"name": name, "reason": "excluded"})
            continue
        if any(part.is_dir() for part in (repo_dir / "retired").glob(f"*/{name}")):
            skipped.append({"name": name, "reason": "already_retired"})
            continue
        located = _locate(repo_dir, name)
        if located is None:
            skipped.append({"name": name, "reason": "not_in_approved"})
            continue
        audience, skill_dir = located
        approved_at = _approved_at(skill_dir)
        idle_days = max(0, (now - approved_at).days)
        if idle_days < min_idle_days:
            skipped.append({
                "name": name, "reason": "approved_too_recent", "audience": audience,
                "approved_at": _ts(approved_at), "idle_days": idle_days,
            })
            continue
        if len(selected) >= limit:
            skipped.append({"name": name, "reason": "over_max"})
            continue
        selected.append({
            "name": name, "holders": row["holders"], "audience": audience,
            "approved_at": _ts(approved_at), "idle_days": idle_days,
        })
    return selected, skipped


# ---------------------------------------------------------------------------
# pr
# ---------------------------------------------------------------------------


def _retirement_record(
    item: dict[str, Any], *, now: datetime, source_commit: str, aggregate: dict[str, Any], aggregate_sha: str
) -> dict[str, Any]:
    return {
        "schema_version": RETIREMENT_SCHEMA,
        "retired_at": _ts(now),
        "reason": "unused",
        "source_commit": source_commit,
        "evidence": {
            "report_generated_at": aggregate["generated_at"],
            "nodes_measured": len(aggregate["nodes"]),
            "min_idle_days": int(item["idle_days"]),
            "aggregate_sha256": aggregate_sha,
        },
    }


def _pr_body(selected: list[dict[str, Any]], *, min_idle_days: int, min_nodes: Any, aggregate_sha: str, aggregate: dict[str, Any]) -> str:
    lines = [
        "## Unused fleet skills → `retired/`",
        "",
        f"Owner decision 2026-10-22 (ccc-node#2239 L1, Wiki DOC-3903 P9): fleet-managed skills with **0 Skill calls on every node** "
        f"that holds them (≥{min_nodes} nodes, telemetry present on each) and **≥{min_idle_days} days since `approved_at`** are moved "
        "from `approved/` to `retired/` (history preserved, `retirement.json` beside each).",
        "",
        "| name | holders | approved_at | idle_days | audience |",
        "|---|---|---|---|---|",
    ]
    for item in selected:
        lines.append(f"| {item['name']} | {item['holders']} | {item['approved_at']} | {item['idle_days']} | {item['audience']} |")
    lines += [
        "",
        f"- thresholds: min_idle_days={min_idle_days} · retire_min_nodes={min_nodes} · nodes measured={len(aggregate['nodes'])} · report generated_at={aggregate['generated_at']}",
        f"- aggregate sha256: `{aggregate_sha}`",
        "- Merging this PR removes the listed skills from every node on its next fleet-skills sync "
        "(`ccc-fleet-skills-sync.py` RETIRE_PRUNE — the node keeps a backup copy). Restore = reverse-move PR "
        "(`retired/<audience>/<name>` → `approved/<audience>/<name>`).",
        "- Needs human approval (#1739). Generated by `scripts/ccc-skill-retire.py`; it never approves or merges.",
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
    ]
    return "\n".join(lines) + "\n"


def _open_pr(
    repo_dir: Path,
    selected: list[dict[str, Any]],
    *,
    repo: str,
    base: str,
    now: datetime,
    aggregate: dict[str, Any],
    aggregate_sha: str,
    min_idle_days: int,
) -> dict[str, Any]:
    source_commit = _head_sha(repo_dir)
    branch = f"retire/auto-{now.strftime('%Y%m%d')}-{aggregate_sha[:8]}"
    if not BRANCH_RE.fullmatch(branch):
        raise RetireError("branch_invalid")
    _run(["git", "checkout", "--quiet", "-b", branch], cwd=repo_dir)
    for item in selected:
        src = Path("approved") / item["audience"] / item["name"]
        dst = Path("retired") / item["audience"] / item["name"]
        (repo_dir / dst).parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "mv", str(src), str(dst)], cwd=repo_dir)
        record = _retirement_record(item, now=now, source_commit=source_commit, aggregate=aggregate, aggregate_sha=aggregate_sha)
        try:
            (repo_dir / dst / "retirement.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            raise RetireError("retirement_write_failed") from None
        _run(["git", "add", str(dst / "retirement.json")], cwd=repo_dir)
    user, email = _gh_identity()
    names = ", ".join(item["name"] for item in selected)
    title = f"retire: {len(selected)} unused fleet skills (≥{min_idle_days}d idle on all nodes, 0 Skill calls)"
    message = (
        f"{title}\n\n{names}\n\nOwner decision 2026-10-22 (ccc-node#2239 L1). aggregate sha256 {aggregate_sha}.\n\n"
        "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n"
    )
    _run(["git", "-c", f"user.name={user}", "-c", f"user.email={email}", "commit", "--quiet", "-m", message], cwd=repo_dir)
    head = _head_sha(repo_dir)
    _run(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo_dir)
    body_path = repo_dir.parent / "pr-body.md"
    try:
        body_path.write_text(
            _pr_body(selected, min_idle_days=min_idle_days, min_nodes=(aggregate.get("thresholds") or {}).get("retire_min_nodes"),
                     aggregate_sha=aggregate_sha, aggregate=aggregate),
            encoding="utf-8",
        )
    except OSError:
        raise RetireError("pr_body_write_failed") from None
    out = _run([
        "gh", "pr", "create", "--repo", repo, "--base", base, "--head", branch,
        "--title", title, "--body-file", str(body_path),
    ]).stdout.decode("utf-8", "replace").strip().splitlines()
    url = out[-1].strip() if out else ""
    if not url.startswith("https://github.com/"):
        raise RetireError("pr_url_unresolved")
    return {"pr_url": url, "branch": branch, "head": head}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _bounded(low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if number < low or number > high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number
    return parse


def _name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid skill name")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fleet skill retirement planner / PR generator (#2239 L1).")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (("plan", "read-only selection with ages"), ("pr", "open the retirement PR (needs --yes)")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("--aggregate", required=True, help="ccc-skill-usage-report.py aggregate --json output")
        p.add_argument("--fleet-skills-dir", help="existing fleet-skills clone (default: fresh clone in a 0700 temp dir)")
        p.add_argument("--min-idle-days", type=_bounded(1, 3650), default=DEFAULT_MIN_IDLE_DAYS,
                       help=f"days since approval.json approved_at (default {DEFAULT_MIN_IDLE_DAYS})")
        p.add_argument("--max", type=_bounded(1, 500), default=DEFAULT_MAX, help=f"at most N skills per PR (default {DEFAULT_MAX})")
        p.add_argument("--exclude", type=_name, action="append", default=[], help="skill name to leave out (repeatable)")
        if command == "pr":
            p.add_argument("--yes", action="store_true", help="actually move, commit, push and open the PR")
            p.add_argument("--base", default=os.environ.get("CCC_SKILL_RETIRE_BASE", DEFAULT_BASE))
    return parser


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    repo = os.environ.get("CCC_SKILL_RETIRE_REPO", DEFAULT_REPO)
    if not REPO_RE.fullmatch(repo):
        raise RetireError("repo_invalid")
    remote = os.environ.get("CCC_SKILL_RETIRE_REMOTE", f"https://github.com/{repo}.git")
    base = getattr(args, "base", DEFAULT_BASE)
    if not BRANCH_RE.fullmatch(base):
        raise RetireError("base_invalid")
    aggregate, aggregate_sha = _load_aggregate(Path(args.aggregate))
    candidates = _candidates(aggregate)
    now = _utc_now()
    apply = args.command == "pr" and bool(getattr(args, "yes", False))
    mode = "plan" if args.command == "plan" else ("pr" if apply else "pr-dry-run")
    tmp: Path | None = None
    try:
        if args.fleet_skills_dir:
            repo_dir = _existing_dir(args.fleet_skills_dir)
            if apply:
                raise RetireError("pr_requires_fresh_clone")
        else:
            tmp = _private_tmpdir()
            repo_dir = _clone(remote, base, tmp)
        selected, skipped = _select(
            repo_dir, candidates, now=now, min_idle_days=args.min_idle_days, limit=args.max, exclude=set(args.exclude),
        )
        result: dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "repo": repo,
            "aggregate_sha256": aggregate_sha,
            "thresholds": {"min_idle_days": args.min_idle_days, "max": args.max},
            "selected": selected,
            "skipped": skipped,
        }
        if not apply:
            return result
        if not selected:
            result["code"] = "nothing_to_retire"
            return result
        result.update(_open_pr(
            repo_dir, selected, repo=repo, base=base, now=now, aggregate=aggregate,
            aggregate_sha=aggregate_sha, min_idle_days=args.min_idle_days,
        ))
        return result
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except RetireError as error:
        print(json.dumps({"ok": False, "code": error.code}, sort_keys=True))
        return 2
    except Exception as error:  # noqa: BLE001 — body-free failure, never a traceback with paths
        print(json.dumps({"ok": False, "code": type(error).__name__}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
