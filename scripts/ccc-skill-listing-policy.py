#!/usr/bin/env python3
"""Deterministic skill-listing budget policy for Claude Code (ccc-node#2011 A).

Claude Code injects a skill listing on every turn, capped at
``skillListingBudgetFraction`` of the context window (default 0.01). When the
listing is over budget it keeps every name but only describes the most-used
skills; everything else is listed as a bare name. A node with ~200 skills and
usage data for a dozen of them therefore got descriptions in effectively
alphabetical order, and the fleet workflows (gh-pr-flow, wiki-record, ...) were
often name-only.

This tool makes the choice explicit:

* a skill KEEPS its description when it is in the repo-shipped core list
  (``claude/skill-listing-core.txt``);
* a skill used within the recent window (``state/skill-usage/usage.jsonl`` and
  ``state/skill-autosave-usage.json``) keeps its description only while the
  estimated listing still fits the budget (ccc-node#2031): recent skills are
  ranked by most recent use (then use count, then name) and described in that
  order until the next one would cross the budget; the rest are
  ``recent, over budget``;
* every other skill gets ``skillOverrides[<name>] = "name-only"`` — still
  listed, still invocable by name.

Safety contract:

* it only ever writes ``"name-only"``; never ``"off"``, never deletes or moves
  a skill (owner decision in ccc-node#1739);
* it only manages override entries it created itself, tracked in
  ``state/skill-listing-policy.json``; an operator-written entry (any key it
  does not own, or an owned key whose value an operator changed) always wins
  and is never modified;
* ``skillListingBudgetFraction`` is set to 0.02 only when the key is absent;
* ``settings.json`` is backed up and replaced atomically, only when the
  rendered result differs (a second ``apply`` is a no-op).

Subcommands: ``plan`` (read-only), ``apply``, ``release`` (remove every entry
this tool owns). Kill switch: ``CCC_SKILL_LISTING_POLICY=0`` or the file
``<claude-dir>/skill-listing-policy.disabled`` makes ``apply`` a no-op.
Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

STATE_SCHEMA = "ccc.skill-listing-policy.v1"
STATE_FILE = "skill-listing-policy.json"
LOCK_FILE = ".skill-listing-policy.lock"
CORE_FILE = "skill-listing-core.txt"
DISABLED_FILE = "skill-listing-policy.disabled"
BACKUP_SUBDIR = "skill-listing-policy"
BACKUP_KEEP = 10

MANAGED_VALUE = "name-only"
OVERRIDES_KEY = "skillOverrides"
BUDGET_KEY = "skillListingBudgetFraction"
MAX_DESC_KEY = "skillListingMaxDescChars"
DEFAULT_BUDGET_FRACTION = 0.01  # Claude Code default when the key is absent
POLICY_BUDGET_FRACTION = 0.02
DEFAULT_MAX_DESC_CHARS = 1536
DEFAULT_DAYS = 30
DEFAULT_CONTEXT_TOKENS = 200_000
CHARS_PER_TOKEN = 4
REASON_OVER_BUDGET = "recent, over budget"

MAX_SETTINGS_BYTES = 4 * 1024 * 1024
MAX_SKILL_MD_BYTES = 256 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_AUTOSAVE_USAGE_BYTES = 4 * 1024 * 1024

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
TRUE_WORDS = {"true", "yes", "on"}


class PolicyError(Exception):
    """A precondition failed; nothing was written."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def claude_dir_from_env() -> Path:
    raw = os.environ.get("CCC_CLAUDE_DIR", "")
    return Path(raw) if raw else Path.home() / ".claude"


def now_utc() -> datetime:
    pinned = os.environ.get("CCC_SKILL_LISTING_POLICY_NOW", "")
    if pinned:
        parsed = parse_ts(pinned)
        if parsed is None:
            raise PolicyError("invalid CCC_SKILL_LISTING_POLICY_NOW")
        return parsed
    return datetime.now(timezone.utc)


def parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_core_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("CCC_SKILL_LISTING_CORE", "")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    # Installed layout (<claude-dir>/hooks/) first, then the repository layout.
    for candidate in (here / CORE_FILE, here.parent / "claude" / CORE_FILE):
        if candidate.is_file():
            return candidate
    return here / CORE_FILE


def load_core(path: Path) -> list[str]:
    """Core names, in file order. A missing core list fails closed: without it
    every workflow skill would be demoted."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"core list unreadable: {path} ({exc.strerror or exc})") from None
    names: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if not NAME_RE.match(entry):
            raise PolicyError(f"core list {path}:{lineno}: invalid skill name")
        if entry not in names:
            names.append(entry)
    return names


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        try:
            decoded = json.loads(value)
        except ValueError:
            return inner
        return decoded if isinstance(decoded, str) else inner
    return value


def parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level scalar keys of a SKILL.md YAML frontmatter block.

    A deliberately small subset (no PyYAML on the fleet): plain, quoted, and
    block (``|``/``>``) scalars, including plain scalars continued on
    indented lines. Nested mappings are skipped. Good enough to size the
    listing and read ``name``/``description``; never used for authority.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []
    block: str | None = None

    def flush() -> None:
        if key is None:
            return
        if block is not None:
            parts = [b.strip() for b in buf]
            joined = ("\n" if block == "|" else " ").join(p for p in parts if p)
            out[key] = joined.strip()
        else:
            out[key] = _unquote(" ".join(b.strip() for b in buf if b.strip()))

    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = KEY_RE.match(line)
        if match and not line[:1].isspace():
            flush()
            key, rest = match.group(1), match.group(2).strip()
            buf = []
            block = None
            if rest[:1] in {"|", ">"}:
                block = rest[0]
            elif rest:
                buf.append(rest)
            continue
        if key is not None and (line[:1].isspace() or not line.strip()):
            buf.append(line)
    flush()
    return out


def discover_skills(skills_dir: Path) -> dict[str, dict[str, Any]]:
    """name -> {desc_chars, listed} for every <skills>/<dir>/SKILL.md."""
    found: dict[str, dict[str, Any]] = {}
    if not skills_dir.is_dir():
        return found
    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        try:
            if not skill_md.is_file() or skill_md.stat().st_size > MAX_SKILL_MD_BYTES:
                continue
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        name = fm.get("name", "").strip() or entry.name
        if not NAME_RE.match(name) or name in found:
            continue
        desc = fm.get("description", "")
        when = fm.get("when_to_use", "")
        text_len = len(desc) + (len(when) + 1 if when else 0)
        listed = fm.get("disable-model-invocation", "").strip().lower() not in TRUE_WORDS
        found[name] = {"desc_chars": text_len, "listed": listed}
    return found


def _read_bounded(path: Path, limit: int) -> bytes | None:
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > limit:
                fh.seek(size - limit)
                data = fh.read()
                # Drop the partial first line of a tail read.
                return data.split(b"\n", 1)[1] if b"\n" in data else b""
            return fh.read()
    except OSError:
        return None


def recent_usage(state_dir: Path, cutoff: datetime) -> dict[str, dict[str, Any]]:
    """skill -> {"ts": latest ISO timestamp at/after ``cutoff``, "count": n}.

    ``count`` is the number of in-window ``usage.jsonl`` records; an in-window
    autosave record counts once. It only breaks ties between equal timestamps.
    """
    latest: dict[str, datetime] = {}
    counts: dict[str, int] = {}

    def note(skill: Any, ts: Any) -> None:
        if not isinstance(skill, str) or not NAME_RE.match(skill):
            return
        when = parse_ts(ts)
        if when is None or when < cutoff:
            return
        counts[skill] = counts.get(skill, 0) + 1
        if skill not in latest or when > latest[skill]:
            latest[skill] = when

    raw = _read_bounded(state_dir / "skill-usage" / "usage.jsonl", MAX_LEDGER_BYTES)
    for line in (raw or b"").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            note(row.get("skill"), row.get("ts"))

    raw = _read_bounded(state_dir / "skill-autosave-usage.json", MAX_AUTOSAVE_USAGE_BYTES)
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    records = doc.get("records") if isinstance(doc, dict) else None
    if isinstance(records, dict):
        for key, rec in records.items():
            if not isinstance(key, str) or not isinstance(rec, dict):
                continue
            provider, sep, name = key.partition(":")
            if sep and provider != "claude":
                continue  # codex/piri/danso lanes list their own skills
            skill = name if sep else key
            stamps = [t for t in (parse_ts(rec.get(f)) for f in ("last_used_at", "last_viewed_at")) if t]
            if stamps:
                note(skill, max(stamps).isoformat())
    return {k: {"ts": v.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": counts[k]} for k, v in latest.items()}


def load_settings(path: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    if path.is_symlink():
        raise PolicyError(f"refusing symlinked settings file: {path}")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise PolicyError(f"settings unreadable: {path} ({exc.strerror or exc})") from None
    if len(raw) > MAX_SETTINGS_BYTES:
        raise PolicyError(f"settings too large: {path}")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PolicyError(f"settings is not valid JSON: {path} (left untouched)") from None
    if not isinstance(doc, dict):
        raise PolicyError(f"settings is not a JSON object: {path} (left untouched)")
    overrides = doc.get(OVERRIDES_KEY)
    if overrides is not None and not isinstance(overrides, dict):
        raise PolicyError(f"{OVERRIDES_KEY} is not an object in {path} (left untouched)")
    return raw, doc


def load_state(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"schema": STATE_SCHEMA, "owned_overrides": {}, "owned_budget_fraction": None}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        raise PolicyError(f"policy state unreadable: {path} (fix or remove it)") from None
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        raise PolicyError(f"policy state has an unknown schema: {path}")
    owned = doc.get("owned_overrides")
    if not isinstance(owned, dict) or any(v != MANAGED_VALUE for v in owned.values()):
        raise PolicyError(f"policy state owned_overrides is malformed: {path}")
    return {
        "schema": STATE_SCHEMA,
        "owned_overrides": dict(owned),
        "owned_budget_fraction": doc.get("owned_budget_fraction"),
    }


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def entry_chars(name: str, desc_chars: int, mode: str, max_desc: int) -> int:
    """Approximate listing cost of one skill: "- name: description\\n"."""
    if mode in {"off", "user-invocable-only"}:
        return 0
    if mode == MANAGED_VALUE:
        return len(name) + 3
    return len(name) + 5 + min(desc_chars, max_desc)


def effective_max_desc(settings: dict[str, Any]) -> int:
    max_desc = settings.get(MAX_DESC_KEY)
    if not isinstance(max_desc, int) or isinstance(max_desc, bool) or max_desc <= 0:
        return DEFAULT_MAX_DESC_CHARS
    return max_desc


def effective_fraction(settings: dict[str, Any]) -> float:
    fraction = settings.get(BUDGET_KEY)
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 < fraction <= 1:
        return DEFAULT_BUDGET_FRACTION
    return float(fraction)


def budget_chars_for(fraction: float, context_tokens: int) -> int:
    return int(fraction * context_tokens * CHARS_PER_TOKEN)


def override_mode(value: Any) -> str:
    """The listing mode an override value stands for (non-strings list as "on")."""
    return value if isinstance(value, str) else "on"


def decide(
    skills: dict[str, dict[str, Any]],
    operator: dict[str, Any],
    core: set[str],
    recent: dict[str, dict[str, Any]],
    release: bool,
    max_desc: int = DEFAULT_MAX_DESC_CHARS,
    budget_chars: int | None = None,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Per-skill decisions and the override entries this tool wants to own.

    Operator entries, core descriptions and plain name-only entries are fixed
    costs. Recently used skills then get descriptions in rank order (latest
    use, then use count, then name) while the running estimate stays within
    ``budget_chars``; from the first one that does not fit, every remaining
    recent skill is name-only (``recent, over budget``), so a less recently
    used skill is never described while a more recent one is not.
    ``budget_chars=None`` (release) keeps every recent skill described.
    """
    decisions: dict[str, dict[str, str]] = {}
    desired: dict[str, str] = {}
    pending: list[str] = []
    used = 0

    def put(name: str, decision: str, reason: str) -> None:
        decisions[name] = {"skill": name, "decision": decision, "reason": reason}
        if decision == MANAGED_VALUE:
            desired[name] = MANAGED_VALUE

    for name in sorted(skills):
        desc_chars = skills[name]["desc_chars"]
        if not skills[name]["listed"]:
            put(name, "unlisted", "disable-model-invocation")
        elif name in operator:
            put(name, "operator", f"operator override {operator[name]!r}")
            used += entry_chars(name, desc_chars, override_mode(operator[name]), max_desc)
        elif name in core:
            put(name, "keep", "core")
            used += entry_chars(name, desc_chars, "on", max_desc)
        elif name in recent and (release or budget_chars is None):
            put(name, "keep", f"used {recent[name]['ts']}")
        elif name in recent:
            pending.append(name)  # provisionally name-only; placed below
            used += entry_chars(name, desc_chars, MANAGED_VALUE, max_desc)
        elif release:
            put(name, "keep", "release")
        else:
            put(name, MANAGED_VALUE, "not core, not recently used")
            used += entry_chars(name, desc_chars, MANAGED_VALUE, max_desc)

    # Stable two-key sort: name ascending, then latest use / count descending.
    pending.sort(key=lambda n: (recent[n]["ts"], recent[n]["count"]), reverse=True)
    fits = True
    for name in pending:
        desc_chars = skills[name]["desc_chars"]
        extra = (entry_chars(name, desc_chars, "on", max_desc)
                 - entry_chars(name, desc_chars, MANAGED_VALUE, max_desc))
        if fits and budget_chars is not None and used + extra <= budget_chars:
            put(name, "keep", f"used {recent[name]['ts']}")
            used += extra
        else:
            fits = False
            put(name, MANAGED_VALUE, f"{REASON_OVER_BUDGET} (used {recent[name]['ts']})")
    return [decisions[name] for name in sorted(decisions)], desired


def apply_budget(
    settings: dict[str, Any],
    new_settings: dict[str, Any],
    owned_budget: Any,
    budget_fraction: float,
    release: bool,
) -> tuple[Any, str]:
    """Set the budget key only when absent; release it only when still ours."""
    if release:
        if BUDGET_KEY in settings and owned_budget is not None and settings[BUDGET_KEY] == owned_budget:
            del new_settings[BUDGET_KEY]
            return None, "released"
        return None, "present" if BUDGET_KEY in settings else "absent"
    if BUDGET_KEY not in settings:
        new_settings[BUDGET_KEY] = budget_fraction
        return budget_fraction, "set"
    if owned_budget is not None and settings[BUDGET_KEY] != owned_budget:
        return None, "present"  # an operator changed it: no longer ours
    return owned_budget, "present"


def compute(
    settings: dict[str, Any],
    state: dict[str, Any],
    skills: dict[str, dict[str, Any]],
    core: list[str],
    recent: dict[str, dict[str, Any]],
    budget_fraction: float,
    release: bool = False,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> dict[str, Any]:
    current: dict[str, Any] = dict(settings.get(OVERRIDES_KEY) or {})
    owned_before: dict[str, str] = state["owned_overrides"]
    # An owned key is still ours only while its value is exactly what we wrote.
    ours_now = {k for k, v in current.items() if owned_before.get(k) == v}
    operator = {k: v for k, v in current.items() if k not in ours_now}
    # Fit against the budget the settings will have AFTER this run (the key may
    # be set by this very run), so a second run sees the same budget.
    probe = dict(settings)
    apply_budget(settings, probe, state.get("owned_budget_fraction"), budget_fraction, release)
    budget_chars = None if release else budget_chars_for(effective_fraction(probe), context_tokens)
    decisions, desired_ours = decide(skills, operator, set(core), recent, release,
                                     effective_max_desc(probe), budget_chars)
    if any(v != MANAGED_VALUE for v in desired_ours.values()):
        raise PolicyError("internal: refusing to write a value other than name-only")

    # Rebuild in a stable order: existing keys keep their position, new owned
    # entries are appended sorted. Operator entries are copied verbatim.
    new_overrides: dict[str, Any] = {}
    for key, value in current.items():
        if key in operator:
            new_overrides[key] = value
        elif key in desired_ours:
            new_overrides[key] = MANAGED_VALUE
    for key in sorted(desired_ours):
        new_overrides.setdefault(key, MANAGED_VALUE)

    new_settings = dict(settings)
    if new_overrides:
        new_settings[OVERRIDES_KEY] = new_overrides
    elif current:
        # Every remaining entry was ours and is gone: drop the key rather than
        # leave {}. An operator-written empty {} (current empty) is left as-is.
        del new_settings[OVERRIDES_KEY]

    owned_budget, budget_action = apply_budget(
        settings, new_settings, state.get("owned_budget_fraction"), budget_fraction, release)
    return {
        "settings": new_settings,
        "state": {
            "schema": STATE_SCHEMA,
            "owned_overrides": {k: MANAGED_VALUE for k in sorted(desired_ours)},
            "owned_budget_fraction": owned_budget,
        },
        "decisions": decisions,
        "operator": operator,
        "added": sorted(k for k in desired_ours if k not in ours_now),
        "removed": sorted(k for k in ours_now if k not in desired_ours),
        "budget_action": budget_action,
    }


def estimate(settings: dict[str, Any], skills: dict[str, dict[str, Any]], context_tokens: int,
             core: set[str] | None = None) -> dict[str, Any]:
    overrides = settings.get(OVERRIDES_KEY) or {}
    max_desc = effective_max_desc(settings)
    fraction = effective_fraction(settings)
    core = core or set()
    described = name_only = 0
    described_chars = name_only_chars = core_desc_chars = 0
    for name, info in skills.items():
        if not info["listed"]:
            continue
        mode = override_mode(overrides.get(name, "on"))
        cost = entry_chars(name, info["desc_chars"], mode, max_desc)
        if mode == MANAGED_VALUE:
            name_only += 1
            name_only_chars += cost
        elif mode not in {"off", "user-invocable-only"}:
            described += 1
            described_chars += cost
            if name in core:
                core_desc_chars += cost
    total = described_chars + name_only_chars
    budget = budget_chars_for(fraction, context_tokens)
    return {
        "listing_chars": total,
        "described": described,
        "name_only": name_only,
        "described_chars": described_chars,
        "name_only_chars": name_only_chars,
        "core_desc_chars": core_desc_chars,
        "budget_fraction": fraction,
        "budget_chars": budget,
        "over_budget": total > budget,
    }


def budget_warning(after: dict[str, Any]) -> str | None:
    """One line naming the dominant cost when the after-estimate is over budget.

    After the fit, every recent non-core skill is already name-only when the
    fixed costs alone exceed the budget, so what is left to trim is the core
    list's descriptions, the installed skill count (name list), or operator
    pins that keep a description.
    """
    if not after["over_budget"]:
        return None
    over = after["listing_chars"] - after["budget_chars"]
    other = after["described_chars"] - after["core_desc_chars"]
    costs = [
        (after["core_desc_chars"], "core descriptions",
         "shorten core SKILL.md descriptions or trim claude/skill-listing-core.txt"),
        (after["name_only_chars"], f"name list ({after['name_only']} name-only skills)",
         "reduce the installed skill count (archive per #1739)"),
        (other, "other kept descriptions", "review operator skillOverrides pins"),
    ]
    chars, label, fix = max(costs, key=lambda c: c[0])
    return (f"still over the estimated budget by {over} chars "
            f"({after['listing_chars']}/{after['budget_chars']}); dominant cost: "
            f"{label} = {chars} chars — {fix}")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def render(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        json.loads(Path(tmp).read_bytes().decode("utf-8"))  # validate what landed
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def backup_settings(claude_dir: Path, settings_path: Path, stamp: str) -> Path:
    backup_dir = claude_dir / "backups" / BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    dest = backup_dir / f"settings.json.{stamp}"
    n = 1
    while dest.exists():
        dest = backup_dir / f"settings.json.{stamp}-{n}"
        n += 1
    shutil.copyfile(settings_path, dest)
    os.chmod(dest, 0o600)
    backups = sorted(backup_dir.glob("settings.json.*"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-BACKUP_KEEP]:
        with contextlib.suppress(OSError):
            old.unlink()
    return dest


@contextlib.contextmanager
def locked(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(state_dir / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def disabled(claude_dir: Path) -> str | None:
    if os.environ.get("CCC_SKILL_LISTING_POLICY", "").strip().lower() in {"0", "false", "off", "no"}:
        return "CCC_SKILL_LISTING_POLICY=0"
    if (claude_dir / DISABLED_FILE).exists():
        return str(claude_dir / DISABLED_FILE)
    return None


def summarize(cmd: str, result: dict[str, Any], before: dict[str, Any], after: dict[str, Any],
              recent_days: int, context_tokens: int) -> list[str]:
    counts: dict[str, int] = {}
    reasons = {"core": 0, "recent": 0, "over": 0}
    for d in result["decisions"]:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        if d["decision"] == "keep":
            reasons["core" if d["reason"] == "core" else "recent"] += 1
        elif d["reason"].startswith(REASON_OVER_BUDGET):
            reasons["over"] += 1
    lines = [
        f"skill-listing-policy {cmd}: skills={len(result['decisions'])} "
        f"keep={counts.get('keep', 0)} (core={reasons['core']} recent<{recent_days}d={reasons['recent']}) "
        f"name-only={counts.get(MANAGED_VALUE, 0)} (recent over budget={reasons['over']}) "
        f"operator={counts.get('operator', 0)} unlisted={counts.get('unlisted', 0)}",
        f"  owned entries: +{len(result['added'])} -{len(result['removed'])}; "
        f"operator entries left untouched: {len(result['operator'])}",
        f"  {BUDGET_KEY}: {result['budget_action']} "
        f"({before['budget_fraction']} -> {after['budget_fraction']})",
        f"  estimated listing chars: before={before['listing_chars']} "
        f"(described={before['described']}) after={after['listing_chars']} "
        f"(described={after['described']}); budget@{context_tokens // 1000}k ctx: "
        f"{before['budget_chars']} -> {after['budget_chars']}",
        f"  after: described chars={after['described_chars']} (core={after['core_desc_chars']}) "
        f"name-only chars={after['name_only_chars']} over_budget={str(after['over_budget']).lower()}",
    ]
    warning = budget_warning(after)
    if warning:
        lines.append(f"  WARNING: {warning}")
    return lines


def print_plan(args: argparse.Namespace, claude_dir: Path, raw: bytes | None,
               settings: dict[str, Any], result: dict[str, Any],
               skills: dict[str, dict[str, Any]], core: set[str]) -> None:
    before = estimate(settings, skills, args.context_tokens, core)
    after = estimate(result["settings"], skills, args.context_tokens, core)
    if args.json:
        print(json.dumps({
            "claude_dir": str(claude_dir), "recent_days": args.days,
            "decisions": result["decisions"], "added": result["added"],
            "removed": result["removed"], "operator": sorted(result["operator"]),
            "budget_action": result["budget_action"], "before": before, "after": after,
            "warning": budget_warning(after),
            "changed": render(result["settings"]) != raw,
        }, indent=2, ensure_ascii=False))
        return
    if not args.summary:
        for d in result["decisions"]:
            print(f"{d['decision']:<9} {d['skill']}  [{d['reason']}]")
    for line in summarize("plan", result, before, after, args.days, args.context_tokens):
        print(line)


def write_result(claude_dir: Path, raw: bytes, result: dict[str, Any], stamp: str) -> str:
    """Persist ``result``; returns a one-line status. Caller holds the lock."""
    settings_path = claude_dir / "settings.json"
    state_path = claude_dir / "state" / STATE_FILE
    payload = render(result["settings"])
    state_payload = render(result["state"])
    if payload == raw:
        try:
            if state_path.read_bytes() == state_payload:
                return "no change"
        except OSError:
            pass
        atomic_write(state_path, state_payload, 0o600)
        return "no settings change (ownership state refreshed)"
    # Claim the union first so a crash between the two writes can never leave
    # an entry we wrote looking operator-owned (sticky forever).
    union = dict(load_state(state_path)["owned_overrides"])
    union.update(result["state"]["owned_overrides"])
    atomic_write(state_path, render(dict(result["state"], owned_overrides=dict(sorted(union.items())))), 0o600)
    # Refuse to overwrite a concurrent writer (bridge /model, doctor --apply).
    if settings_path.read_bytes() != raw:
        raise PolicyError("settings.json changed while planning; nothing written, retry later")
    backup = backup_settings(claude_dir, settings_path, stamp)
    atomic_write(settings_path, payload, settings_path.stat().st_mode & 0o777)
    atomic_write(state_path, state_payload, 0o600)
    return f"settings.json updated (backup={backup})"


def run(args: argparse.Namespace) -> int:
    claude_dir = claude_dir_from_env()
    state_dir = claude_dir / "state"
    settings_path = claude_dir / "settings.json"
    cmd = args.cmd

    if cmd == "apply" and (why := disabled(claude_dir)):
        print(f"skill-listing-policy: disabled by {why}; nothing changed")
        return 0

    # release needs no core list: it only removes what this tool owns.
    core = [] if cmd == "release" else load_core(resolve_core_path(args.core))
    now = now_utc()
    recent = recent_usage(state_dir, now - timedelta(days=args.days))
    skills = discover_skills(claude_dir / "skills")

    def evaluate() -> tuple[bytes | None, dict[str, Any] | None, dict[str, Any] | None]:
        raw, settings = load_settings(settings_path)
        if settings is None:
            return None, None, None
        result = compute(settings, load_state(state_dir / STATE_FILE), skills, core, recent,
                         args.budget_fraction, release=(cmd == "release"),
                         context_tokens=args.context_tokens)
        return raw, settings, result

    if cmd == "plan":
        raw, settings, result = evaluate()
        if settings is None or result is None:
            print(f"skill-listing-policy plan: {settings_path} absent — nothing to manage")
        else:
            print_plan(args, claude_dir, raw, settings, result, skills, set(core))
        return 0

    with locked(state_dir):
        raw, settings, result = evaluate()
        if settings is None or result is None or raw is None:
            print(f"skill-listing-policy {cmd}: {settings_path} absent — nothing to manage")
            return 0
        status = write_result(claude_dir, raw, result, now.strftime("%Y%m%dT%H%M%SZ"))
    print(f"skill-listing-policy {cmd}: {status}")
    if not args.quiet:
        before = estimate(settings, skills, args.context_tokens, set(core))
        after = estimate(result["settings"], skills, args.context_tokens, set(core))
        for line in summarize(cmd, result, before, after, args.days, args.context_tokens):
            print(line)
    return 0


def positive_days(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 3650:
        raise argparse.ArgumentTypeError("days must be 1..3650")
    return value


def context_tokens(raw: str) -> int:
    value = int(raw)
    if not 1_000 <= value <= 10_000_000:
        raise argparse.ArgumentTypeError("context tokens must be 1000..10000000")
    return value


def fraction(raw: str) -> float:
    value = float(raw)
    if not 0 < value <= 1:
        raise argparse.ArgumentTypeError("budget fraction must be in (0, 1]")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    defaults: dict[str, Any] = {"days": DEFAULT_DAYS, "context_tokens": DEFAULT_CONTEXT_TOKENS}
    for key, env, check in (("days", "CCC_SKILL_LISTING_RECENT_DAYS", positive_days),
                            ("context_tokens", "CCC_SKILL_LISTING_CONTEXT_TOKENS", context_tokens)):
        raw = os.environ.get(env, "")
        if raw:
            try:
                defaults[key] = check(raw)
            except (argparse.ArgumentTypeError, ValueError) as exc:
                print(f"skill-listing-policy: invalid {env}: {exc}", file=sys.stderr)
                return 2
    for name, help_text in (
        ("plan", "read-only: print per-skill decisions and estimated listing chars"),
        ("apply", "write skillOverrides/budget into settings.json (idempotent)"),
        ("release", "remove every override entry (and budget key) this tool owns"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--core", help=f"core list (default: sibling {CORE_FILE})")
        p.add_argument("--days", type=positive_days, default=defaults["days"],
                       help="recent-use window in days (default 30; env CCC_SKILL_LISTING_RECENT_DAYS)")
        p.add_argument("--budget-fraction", type=fraction, default=POLICY_BUDGET_FRACTION,
                       help=f"{BUDGET_KEY} to set when the key is absent (default 0.02)")
        p.add_argument("--context-tokens", type=context_tokens,
                       default=defaults["context_tokens"],
                       help="context window the budget (and the recent-skill fit) is computed "
                            "for (default 200000; env CCC_SKILL_LISTING_CONTEXT_TOKENS)")
        p.add_argument("--quiet", action="store_true", help="apply/release: one status line only")
        if name == "plan":
            p.add_argument("--summary", action="store_true", help="omit per-skill decision lines")
            p.add_argument("--json", action="store_true", help="machine-readable plan")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except PolicyError as exc:
        print(f"skill-listing-policy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
