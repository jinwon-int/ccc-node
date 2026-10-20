#!/usr/bin/env python3
"""Pre-validate skill trees before they ship (ccc-node#2386).

A skill whose frontmatter is malformed is dropped silently at load, a skill
nobody's always-on text names is rarely triggered, a SKILL.md that tells the
agent to run a script that is not there fails only at use time, and a
malformed hook never fires instead of erroring. This read-only validator
catches those shapes up front. Rules (rule id -> default severity):

* ``frontmatter-missing`` (error): SKILL.md does not open with a ``---``
  block, or the block is never closed.
* ``description-missing`` (error): no ``description:`` or an empty one.
* ``name-missing`` (warn): no ``name:`` (Claude Code falls back to the
  directory name). ``name-mismatch`` (error): ``name:`` differs from the
  skill directory name.
* ``description-banned-word`` (warn): the description uses measurement
  vocabulary (benchmark / evaluation / grader / rubric / score) that invites
  grader-gaming wording.
* ``trigger-core-missing`` (error): a skill in the core listing set
  (``claude/skill-listing-core.txt``) whose directory name never appears
  literally in the always-on text (CLAUDE.md, tools-cheatsheet.md, the output
  styles). ``trigger-missing`` (note): the same for a non-core skill.
* ``script-ref-missing`` (error): SKILL.md runs a ``scripts/<x>.py|.sh``
  anchored to its own skill directory (``$SKILL_DIR/``, ``<skill-root>/``,
  ``.../skills/<name>/``) and the file is not there.
* ``script-ref-unresolved`` (warn): a bare or otherwise anchored
  ``scripts/<x>`` reference found neither in the skill directory nor under any
  script base (the repo root, or ``~/.claude`` on a node).
* ``py-compile`` (error): a ``*.py`` inside a skill directory, or a resolved
  ``.py`` reference, does not compile.
* ``hooks-schema`` (error): a settings/hooks JSON file whose ``hooks`` block
  is not ``{event: [{matcher?, hooks: [{type, command, ...}]}]}``, has an
  empty command, a non-string matcher or a non-positive timeout.
  ``hooks-unknown-event`` / ``hooks-unknown-type`` (warn): an event name or
  hook type outside the known set (a typo never fires).

``--strict`` turns every warning into an error; notes never fail.
``--warn-rule RULE`` demotes an error rule to a warning (CI uses it for rules
the shipped tree does not satisfy yet). Exit status: 0 clean, 1 failing
findings, 2 usage error. Output is sorted and stable; ``--json`` emits one
object. ``--autotrigger --dry-run`` prints a suggested trigger line for every
core skill without one; applying it is a PR, never this tool.

Modes: with no ``--root`` the repo layout is checked (``skills/``,
``codex/skills/``, ``piri/skills/`` plus the repo's settings/hook templates and
always-on text). With ``--root PATH`` (repeatable; e.g. ``~/.claude/skills``)
the node layout under ``--claude-dir`` (default ``$CCC_CLAUDE_DIR`` or
``~/.claude``) supplies the always-on text and ``settings.json``; the script
bases are that dir plus the checkout this file runs from. Dot-directories
(install staging) are skipped. Stdlib only; never writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_REPO = Path(__file__).resolve().parents[1]

SEVERITIES = ("error", "warn", "note")
DEFAULT_SEVERITY = {
    "frontmatter-missing": "error",
    "description-missing": "error",
    "name-missing": "warn",
    "name-mismatch": "error",
    "description-banned-word": "warn",
    "trigger-core-missing": "error",
    "trigger-missing": "note",
    "script-ref-missing": "error",
    "script-ref-unresolved": "warn",
    "py-compile": "error",
    "hooks-schema": "error",
    "hooks-unknown-event": "warn",
    "hooks-unknown-type": "warn",
}

REPO_SKILL_ROOTS = ("skills", "codex/skills", "piri/skills")
REPO_HOOK_FILES = (
    "claude/settings.base.json",
    "claude/hooks/hooks.json",
    "claude/hooks/enforcement-overlay.json",
)
REPO_TRIGGER_FILES = ("claude/CLAUDE.md.template", "claude/hooks/tools-cheatsheet.md")
REPO_OUTPUT_STYLES = "claude/output-styles"
CORE_LIST = "claude/skill-listing-core.txt"

BANNED_WORDS = re.compile(r"\b(benchmark|evaluation|grader|rubric|score)s?\b", re.IGNORECASE)

# Claude Code hook events. Unknown names are a warning, not an error, so a new
# upstream event does not break CI before this list learns it.
HOOK_EVENTS = frozenset(
    {
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "Notification",
        "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop", "StopFailure",
        "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
        "PermissionRequest", "PermissionDenied", "Setup", "TeammateIdle",
        "TaskCreated", "TaskCompleted", "ConfigChange", "InstructionsLoaded",
        "WorktreeCreate", "WorktreeRemove", "Elicitation", "ElicitationResult",
        "CwdChanged", "FileChanged",
    }
)
# hook type -> the field that must be a non-empty string
HOOK_TYPES = {"command": "command", "prompt": "prompt", "agent": "prompt", "http": "url"}

# Characters that end a path-like token in markdown / shell text.
_TOKEN_SPLIT = re.compile(r"[\s`'\"()\[\]|;,=*]+")
_SCRIPT_IN_TOKEN = re.compile(r"(?:^|(?<=/))scripts/[\w.+-]+(?:/[\w.+-]+)*\.(?:py|sh)(?![\w.-])")
_SKILL_DIR_VAR = re.compile(r"skill[_-]?(?:dir|root)", re.IGNORECASE)
_SKILLS_NAME = re.compile(r"(?:^|/)skills/([\w.+-]+)/$")
_FM_KEY = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    severity: str
    message: str
    skill: str = ""

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "path": self.path,
            "line": self.line,
            "skill": self.skill,
            "message": self.message,
        }


@dataclass
class Skill:
    name: str
    root: Path
    directory: Path
    skill_md: Path
    fields: dict = field(default_factory=dict)  # key -> (value, line)
    body: list = field(default_factory=list)  # (line_no, text)


@dataclass
class Config:
    roots: list
    hook_files: list
    trigger_files: list
    core_list: Path | None
    script_bases: list
    strict: bool = False
    warn_rules: frozenset = frozenset()


class Report:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.findings: set[Finding] = set()

    def add(self, rule: str, path: Path, line: int, message: str, skill: str = "") -> None:
        severity = DEFAULT_SEVERITY[rule]
        if severity == "error" and rule in self.config.warn_rules:
            severity = "warn"
        if severity == "warn" and self.config.strict:
            severity = "error"
        self.findings.add(Finding(display(path), line, rule, severity, message, skill))

    def sorted(self) -> list[Finding]:
        return sorted(self.findings)

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)


def display(path: Path) -> str:
    """cwd-relative when possible, else ``~``-abbreviated (no host paths)."""
    path = Path(os.path.abspath(path))
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        pass
    home = Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- frontmatter


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        return inner.replace("''", "'") if raw[0] == "'" else inner.replace('\\"', '"')
    return raw


def parse_frontmatter(lines: list[str]) -> tuple[dict, int] | None:
    """Top-level ``key: value`` fields of a leading ``---`` block.

    Returns ({key: (value, line_no)}, closing_index) or None when there is no
    terminated block. Block scalars (``>``/``|``) and indented continuation
    lines fold into the preceding key; nested mappings are kept as raw text.
    """
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict = {}
    current = None
    parts: list[str] = []
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.strip() == "---":
            if current:
                fields[current] = (_fold(parts), fields[current][1])
            return fields, idx
        match = _FM_KEY.match(line)
        if match and not line[:1].isspace():
            if current:
                fields[current] = (_fold(parts), fields[current][1])
            current = match.group(1)
            fields[current] = ("", idx + 1)
            parts = [match.group(2)]
        elif current:
            parts.append(line)
    return None


def _fold(parts: list[str]) -> str:
    head = parts[0].strip() if parts else ""
    rest = [p.strip() for p in parts[1:] if p.strip()]
    if head in {">", "|", ">-", "|-", ">+", "|+"}:
        return " ".join(rest)
    return _unquote(" ".join([head, *rest]).strip())


def load_skill(root: Path, directory: Path) -> Skill:
    skill_md = directory / "SKILL.md"
    skill = Skill(directory.name, root, directory, skill_md)
    lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    parsed = parse_frontmatter(lines)
    if parsed is None:
        skill.fields = {"__invalid__": ("", 1)}
        skill.body = list(enumerate(lines, 1))
        return skill
    skill.fields, end = parsed
    skill.body = [(i + 1, lines[i]) for i in range(end + 1, len(lines))]
    return skill


def check_frontmatter(skill: Skill, report: Report) -> None:
    md, name = skill.skill_md, skill.name
    if "__invalid__" in skill.fields:
        report.add("frontmatter-missing", md, 1, "SKILL.md must open with a terminated --- block", name)
        return
    desc, desc_line = skill.fields.get("description", ("", 1))
    if "description" not in skill.fields:
        report.add("description-missing", md, 1, "no description: (the skill is dropped at load)", name)
    elif not desc.strip():
        report.add("description-missing", md, desc_line, "description: is empty", name)
    if "name" not in skill.fields:
        report.add("name-missing", md, 1, f"no name: (expected {name!r})", name)
    else:
        value, line = skill.fields["name"]
        if value.strip() != name:
            report.add("name-mismatch", md, line, f"name: {value.strip()!r} != directory {name!r}", name)
    for word in sorted({m.group(0).lower() for m in BANNED_WORDS.finditer(desc)}):
        report.add(
            "description-banned-word", md, desc_line,
            f"description uses measurement word {word!r} (invites grader-gaming wording)", name,
        )


# ---------------------------------------------------------------- triggers


def read_core_list(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def always_on_text(paths: list[Path]) -> str:
    chunks = []
    for path in paths:
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def has_trigger(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def check_triggers(skills: list[Skill], config: Config, report: Report) -> list[dict]:
    """Core skills need a literal trigger; returns autotrigger suggestions."""
    core = set(read_core_list(config.core_list))
    text = always_on_text(config.trigger_files)
    first: dict[str, Skill] = {}
    for skill in skills:
        first.setdefault(skill.name, skill)
    suggestions = []
    for name in sorted(first):
        skill = first[name]
        if has_trigger(name, text):
            continue
        if name in core:
            report.add(
                "trigger-core-missing", skill.skill_md, 1,
                f"core skill {name!r} is not named in CLAUDE.md/tools-cheatsheet/output-style", name,
            )
            suggestions.append({"skill": name, "line": suggest_trigger(name, skill)})
        else:
            report.add("trigger-missing", skill.skill_md, 1, f"skill {name!r} has no trigger line", name)
    return suggestions


def suggest_trigger(name: str, skill: Skill) -> str:
    desc = " ".join(skill.fields.get("description", ("", 0))[0].split())
    stripped = re.sub(r"(?i)^use (?:this skill )?when(?:ever)?\s+", "", desc)
    sentence = re.split(r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.;])\s|\s[—–]\s", stripped, maxsplit=1)[0].rstrip(".;")
    if len(sentence) > 160:
        sentence = sentence[:157].rstrip() + "..."
    if stripped == desc:
        return f'- When the task matches "{sentence}", load the {name} skill.'
    return f"- When {sentence}, load the {name} skill."


# ---------------------------------------------------------------- scripts


def iter_script_refs(skill: Skill):
    """(line_no, prefix, rel) for each ``scripts/<x>.py|.sh`` in the body."""
    for line_no, text in skill.body:
        for token in _TOKEN_SPLIT.split(text):
            match = _SCRIPT_IN_TOKEN.search(token)
            if match:
                yield line_no, token[: match.start()], match.group(0)


def classify(prefix: str, skill: Skill) -> tuple[str, Path | None]:
    """('skill', dir) | ('sibling', dir) | ('relative', dir) | ('external', None) | ('skip', None)."""
    if "://" in prefix:
        return "skip", None
    if prefix in ("", "./"):
        return "relative", skill.directory
    named = _SKILLS_NAME.search(prefix)
    if named:
        if named.group(1) == skill.name:
            return "skill", skill.directory
        return "sibling", skill.root / named.group(1)
    if _SKILL_DIR_VAR.search(prefix):
        return "skill", skill.directory
    if prefix.startswith(("$", "~", "/")) or "$" in prefix:
        return "external", None
    return "relative", skill.directory / prefix


def check_script_refs(skill: Skill, config: Config, report: Report, compiled: dict) -> None:
    seen = set()
    for line_no, prefix, rel in iter_script_refs(skill):
        if (prefix, rel) in seen:
            continue
        seen.add((prefix, rel))
        kind, base = classify(prefix, skill)
        if kind == "skip":
            continue
        shown = prefix + rel
        if kind == "skill":
            target = base / rel
            if not target.is_file():
                report.add(
                    "script-ref-missing", skill.skill_md, line_no,
                    f"{shown} is not in the skill directory ({rel})", skill.name,
                )
                continue
        else:
            candidates = [base / rel] if base is not None else []
            candidates += [b / rel for b in config.script_bases]
            target = next((c for c in candidates if c.is_file()), None)
            if target is None:
                report.add(
                    "script-ref-unresolved", skill.skill_md, line_no,
                    f"{shown} not found in the skill directory or any script base", skill.name,
                )
                continue
        if target.suffix == ".py":
            compile_file(target, report, compiled, skill.name)


def compile_file(path: Path, report: Report, compiled: dict, skill: str = "") -> None:
    key = os.path.realpath(path)
    if key in compiled:
        return
    compiled[key] = True
    try:
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None) or 1
        msg = getattr(exc, "msg", None) or str(exc)
        report.add("py-compile", path, line, f"does not compile: {msg}", skill)


def check_skill_python(skill: Skill, report: Report, compiled: dict) -> None:
    for path in sorted(skill.directory.rglob("*.py")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        compile_file(path, report, compiled, skill.name)


# ---------------------------------------------------------------- hooks


def check_hook_file(path: Path, report: Report) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.add("hooks-schema", path, 1, f"not valid JSON: {exc}")
        return
    if not isinstance(data, dict):
        report.add("hooks-schema", path, 1, "top level must be a JSON object")
        return
    hooks = data.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        report.add("hooks-schema", path, 1, "hooks must be an object of event -> matcher groups")
        return
    for event in sorted(hooks):
        where = f"hooks.{event}"
        if event not in HOOK_EVENTS:
            report.add("hooks-unknown-event", path, 1, f"{where}: unknown hook event {event!r}")
        groups = hooks[event]
        if not isinstance(groups, list):
            report.add("hooks-schema", path, 1, f"{where} must be a list of matcher groups")
            continue
        for gi, group in enumerate(groups):
            check_hook_group(path, f"{where}[{gi}]", group, report)


def check_hook_group(path: Path, where: str, group, report: Report) -> None:
    if not isinstance(group, dict):
        report.add("hooks-schema", path, 1, f"{where} must be an object")
        return
    if "matcher" in group and not isinstance(group["matcher"], str):
        report.add("hooks-schema", path, 1, f"{where}.matcher must be a string")
    entries = group.get("hooks")
    if not isinstance(entries, list) or not entries:
        report.add("hooks-schema", path, 1, f"{where}.hooks must be a non-empty list")
        return
    for hi, hook in enumerate(entries):
        check_hook_entry(path, f"{where}.hooks[{hi}]", hook, report)


def check_hook_entry(path: Path, where: str, hook, report: Report) -> None:
    if not isinstance(hook, dict):
        report.add("hooks-schema", path, 1, f"{where} must be an object")
        return
    kind = hook.get("type")
    if not isinstance(kind, str) or not kind:
        report.add("hooks-schema", path, 1, f"{where}.type is missing (expected 'command')")
        return
    if kind not in HOOK_TYPES:
        report.add("hooks-unknown-type", path, 1, f"{where}.type {kind!r} is not a known hook type")
    else:
        key = HOOK_TYPES[kind]
        value = hook.get(key)
        if not isinstance(value, str) or not value.strip():
            report.add("hooks-schema", path, 1, f"{where}.{key} is empty (the hook never fires)")
    timeout = hook.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        report.add("hooks-schema", path, 1, f"{where}.timeout must be a positive number")


# ---------------------------------------------------------------- driver


def discover_skills(roots: list[Path]) -> list[Skill]:
    skills = []
    for root in roots:
        if not root.is_dir():
            continue
        # dot-directories are install staging (.stage-<name>), not skills
        dirs = (p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        for directory in sorted(dirs):
            if (directory / "SKILL.md").is_file():
                skills.append(load_skill(root, directory))
    return skills


def run(config: Config) -> tuple[Report, list[dict], int]:
    report = Report(config)
    skills = discover_skills(config.roots)
    compiled: dict = {}
    for skill in skills:
        check_frontmatter(skill, report)
        check_script_refs(skill, config, report, compiled)
        check_skill_python(skill, report, compiled)
    suggestions = check_triggers(skills, config, report)
    for hook_file in config.hook_files:
        if hook_file.is_file():
            check_hook_file(hook_file, report)
    return report, suggestions, len(skills)


def build_config(args: argparse.Namespace) -> Config:
    warn_rules = frozenset(args.warn_rule or ())
    if args.root:
        claude_dir = Path(
            os.path.expanduser(args.claude_dir or os.environ.get("CCC_CLAUDE_DIR") or "~/.claude")
        )
        roots = [Path(os.path.expanduser(r)) for r in args.root]
        hook_files = [claude_dir / "settings.json"]
        trigger_files = [claude_dir / "CLAUDE.md", claude_dir / "hooks" / "tools-cheatsheet.md"]
        styles = claude_dir / "output-styles"
        bases = [claude_dir]
        if (SCRIPT_REPO / "setup.sh").is_file() and (SCRIPT_REPO / "scripts").is_dir():
            bases.append(SCRIPT_REPO)  # run from a checkout: repo scripts/ resolve too
        core = SCRIPT_REPO / CORE_LIST
        if not core.is_file():
            core = claude_dir / "skill-listing-core.txt"
    else:
        repo = Path(os.path.expanduser(args.repo_root)) if args.repo_root else SCRIPT_REPO
        roots = [repo / r for r in REPO_SKILL_ROOTS]
        hook_files = [repo / f for f in REPO_HOOK_FILES]
        trigger_files = [repo / f for f in REPO_TRIGGER_FILES]
        styles = repo / REPO_OUTPUT_STYLES
        bases = [repo]
        core = repo / CORE_LIST
    if styles.is_dir():
        trigger_files += sorted(styles.glob("*.md"))
    if args.settings:
        hook_files = [Path(os.path.expanduser(p)) for p in args.settings]
    if args.trigger_file:
        trigger_files = [Path(os.path.expanduser(p)) for p in args.trigger_file]
    if args.script_base:
        bases = [Path(os.path.expanduser(p)) for p in args.script_base]
    if args.core_list:
        core = Path(os.path.expanduser(args.core_list))
    return Config(roots, hook_files, trigger_files, core, bases, args.strict, warn_rules)


def render_text(report: Report, suggestions: list[dict], n_skills: int, autotrigger: bool) -> str:
    out = []
    for f in report.sorted():
        out.append(f"{f.severity.upper():5} {f.rule} {f.path}:{f.line}: {f.message}")
    if autotrigger:
        out.append("autotrigger suggestions (dry-run; apply through a PR):")
        out.extend(f"  {s['skill']}: {s['line']}" for s in suggestions)
        if not suggestions:
            out.append("  (none — every core skill has a trigger line)")
    roots = len(report.config.roots)
    out.append(
        f"skill-validate: {n_skills} skill(s) in {roots} root(s) — "
        f"{report.count('error')} error(s), {report.count('warn')} warning(s), "
        f"{report.count('note')} note(s){' [strict]' if report.config.strict else ''}"
    )
    return "\n".join(out)


def render_json(report: Report, suggestions: list[dict], n_skills: int, autotrigger: bool) -> str:
    payload = {
        "version": 1,
        "strict": report.config.strict,
        "roots": [display(r) for r in report.config.roots],
        "skills_checked": n_skills,
        "summary": {s: report.count(s) for s in SEVERITIES},
        "findings": [f.as_dict() for f in report.sorted()],
    }
    if autotrigger:
        payload["autotrigger"] = suggestions
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only skill pre-validator (#2386).")
    p.add_argument("--root", action="append", help="skill root to check (repeatable; node mode)")
    p.add_argument("--repo-root", help="repo checkout for the default repo mode")
    p.add_argument("--claude-dir", help="node mode: Claude dir (default $CCC_CLAUDE_DIR or ~/.claude)")
    p.add_argument("--settings", action="append", help="settings/hooks JSON to check (repeatable)")
    p.add_argument("--trigger-file", action="append", help="always-on text file (repeatable)")
    p.add_argument("--script-base", action="append", help="base dir for scripts/ refs (repeatable)")
    p.add_argument("--core-list", help="core skill list (default claude/skill-listing-core.txt)")
    p.add_argument("--strict", action="store_true", help="warnings fail too")
    p.add_argument("--warn-rule", action="append", choices=sorted(DEFAULT_SEVERITY),
                   help="demote an error rule to a warning (repeatable)")
    p.add_argument("--json", action="store_true", help="emit one JSON object")
    p.add_argument("--autotrigger", action="store_true", help="suggest trigger lines (needs --dry-run)")
    p.add_argument("--dry-run", action="store_true", help="print only; this tool never writes")
    args = p.parse_args(argv)
    if args.autotrigger and not args.dry_run:
        p.error("--autotrigger only supports --dry-run (apply the suggestion through a PR)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config = build_config(args)
    missing = [r for r in config.roots if not r.is_dir()]
    if args.root and missing:
        print(f"skill-validate: root not found: {display(missing[0])}", file=sys.stderr)
        return 2
    report, suggestions, n_skills = run(config)
    render = render_json if args.json else render_text
    print(render(report, suggestions, n_skills, args.autotrigger))
    return 1 if report.count("error") else 0


if __name__ == "__main__":
    sys.exit(main())
