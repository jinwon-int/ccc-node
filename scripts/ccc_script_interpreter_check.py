#!/usr/bin/env python3
"""Fail CI on repo shell scripts invoked without naming an interpreter (#1160).

Exec'ing a repo ``*.sh`` through its shebang silently dies on Termux/Android:
that platform has no ``/bin/bash`` and no ``/usr`` at all, so the exec returns
126/127 and the failure hides behind fail-open branches, ``except OSError``,
or ``$!`` bookkeeping. Five defects of this exact shape shipped silently
(#472, #663, #1151, #1157, #1159) and each was fixed individually; this check
keeps the NEXT call site from re-entering the same form.

What is flagged
---------------
Shell (tracked ``*.sh``) — a ``*.sh`` path as the effective command word with
no interpreter in front of it::

    "$HOOKDIR/scan-injection.sh" "$label"          # command position
    nohup "$SCRIPT_DIR/start.sh" &                  # launcher prefix (#1151)
    exec "$DIR/foo.sh"                              # exec prefix
    out="$(printf x | "$DIR/foo.sh")"               # after a pipe (#1157 form)

Launcher prefixes stripped before the decision: nohup, exec, setsid, command,
builtin, time, sudo, env — plus leading VAR=value assignments and control
keywords (if/then/while/do/!/...). Command substitutions ``$( ... )`` and
backticks are scanned recursively as their own command levels.

Python (tracked ``*.py``) — ``subprocess.{run,Popen,call,check_output,
check_call}`` whose argv list head statically resolves to a ``*.sh`` path
(literal, f-string, os.path.join, or a simply-assigned name), e.g.
``subprocess.Popen([tool_path, query])`` with ``tool_path = f"{h}/search.sh"``
(#1159 form).

What is intentionally NOT flagged (false-positive guards)
---------------------------------------------------------
- an interpreter is already named: ``bash "$x"``, ``setsid bash "$x"``
  (claude/hooks/lib/spawn-detached.sh is the canonical precedent)
- sourcing: ``. "$x"`` / ``source "$x"``
- the path is an argument, not the command: ``[ -x "$x" ]``, ``cp "$x" dest``,
  ``sed -i "s|$HERE/a.sh|...|"`` — only the effective command word is tested
- assignments: ``SCAN="$DIR/scan.sh"`` (the value is data until used)
- operator/test override seams: a token carrying ``${VAR:-default}`` where VAR
  is a known seam (CCC_SCAN_INJECTION_BIN, CCC_BRIDGE_RESTART_SPAWN) or matches
  an override-style suffix (_BIN/_SPAWN/_CMD/_COMMAND/_TOOL/_OVERRIDE) must
  exec exactly as named — forcing an interpreter onto it would break the seam
  (#1152/#1158 keep this distinction)
- comment lines, here-doc bodies (data, not code), array-literal elements
- a command word that does not statically resolve to a ``*.sh`` path
  (``"$scan_bin"``, ``"${launcher[@]}"``): the check cannot prove the target
  is a repo script, so it stays silent — the seam/waiver conventions are how
  those call sites document intent
- python argv heads that are system tools (``tar``, ``git``) or names that do
  not statically resolve to a ``*.sh`` path

Exceptions
----------
A deliberate violation (e.g. a test that execs a bad-shebang stub on purpose
to pin the OSError path) carries an inline waiver WITH a reason on the same
line::

    "$TMP/bad.sh" >/dev/null 2>&1  # ccc:interpreter-ok: pins the exec-failure path (#1159)

A waiver without a non-empty reason does not suppress the finding.

CLI: ``ccc_script_interpreter_check.py [--repo-root DIR]``
Exit 0 = clean, 1 = findings, 2 = usage/internal error.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import re
import subprocess
import sys
from pathlib import Path

WAIVER_RE = re.compile(r"ccc:interpreter-ok:\s*\S")
WAIVER_MARK = "ccc:interpreter-ok"

# Override seams: the operator/test named the executable; it may not be a bash
# script at all, so it must exec exactly as given (#1152, #1158).
SEAM_VARS = frozenset({
    "CCC_SCAN_INJECTION_BIN",
    "CCC_BRIDGE_RESTART_SPAWN",
})
SEAM_SUFFIXES = ("_BIN", "_SPAWN", "_CMD", "_COMMAND", "_TOOL", "_OVERRIDE")

INTERPRETERS = frozenset({
    "bash", "sh", "dash", "zsh", "ksh", "ash", "env",
    "python", "python3", "perl", "ruby", "node",
    "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/env",
    "$SHELL", "${SHELL}",
})

# Launcher prefixes that may sit between the command start and the real
# command word (#1151 was `nohup "$SCRIPT_DIR/start.sh"`).
LAUNCHERS = frozenset({
    "nohup", "exec", "setsid", "command", "builtin", "time", "sudo", "env",
})
CONTROL_WORDS = frozenset({
    "if", "then", "elif", "else", "while", "until", "do", "done", "for", "in",
    "case", "esac", "select", "!", "{", "}", "coproc", "function",
})
# Declaration keywords: following words are assignments/arguments, never the
# executed command (export FOO=1 / local x="$D/f.sh").
DECL_WORDS = frozenset({"local", "declare", "typeset", "export", "readonly"})

ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
ARRAY_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=\($")
SEAM_DEFAULT_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-")
REDIR_RE = re.compile(r"^\d*(>>?|<<?)&?\d*$|^<<-?['\"]?[A-Za-z_]")

HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

SUBPROCESS_FUNCS = frozenset({"run", "Popen", "call", "check_output", "check_call"})


class Finding:
    __slots__ = ("path", "line", "kind", "message")

    def __init__(self, path: str, line: int, kind: str, message: str):
        self.path = path
        self.line = line
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.message}"


# ---------------------------------------------------------------------------
# Shell scanning
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> tuple[str, str]:
    """Split a shell line into (code, comment) honouring quotes.

    A '#' starts a comment only where it begins a word (start of line or after
    whitespace) outside single/double quotes; ${x#pat} and "a#b" stay code.
    """
    sq = dq = esc = False
    for i, ch in enumerate(line):
        if esc:
            esc = False
            continue
        if ch == "\\" and not sq:
            esc = True
            continue
        if ch == "'" and not dq:
            sq = not sq
            continue
        if ch == '"' and not sq:
            dq = not dq
            continue
        if ch == "#" and not sq and not dq and (i == 0 or line[i - 1] in " \t"):
            return line[:i], line[i:]
    return line, ""


def _heredoc_body_lines(lines: list[str]) -> set[int]:
    """1-based line numbers that are here-doc bodies/terminators (data)."""
    data: set[int] = set()
    terms: list[tuple[str, bool]] = []  # (terminator, is_dash)
    for lineno, raw in enumerate(lines, 1):
        if terms:
            term, dash = terms[0]
            done = (raw.lstrip("\t") == term or raw.strip() == term) if dash else (raw == term)
            data.add(lineno)
            if done:
                terms.pop(0)
            continue
        code, _ = _strip_comment(raw)
        for m in HEREDOC_RE.finditer(code):
            terms.append((m.group(2), m.group(0).startswith("<<-")))
    return data


def _logical_spans(coded: list[tuple[int, str | None]]):
    """Join physical lines into logical command texts.

    `coded` is (lineno, code-or-None-for-heredoc-data). Lines are joined on
    trailing-backslash continuations and on unterminated quotes (a newline
    inside quotes is kept). Yields (text, marks) where marks is a sorted list
    of (offset, lineno) for offset->line mapping.
    """
    spans: list[tuple[str, list[tuple[int, int]]]] = []
    buf: list[str] = []
    marks: list[tuple[int, int]] = []
    sq = dq = False

    def emit() -> None:
        nonlocal buf, marks
        if buf or marks:
            text = "".join(buf)
            if text.strip():
                spans.append((text, marks))
        buf, marks = [], []

    for lineno, code in coded:
        if code is None:
            emit()  # heredoc data cannot be mid-quote in sane code; be safe
            continue
        if not buf:
            marks = [(0, lineno)]
        else:
            marks.append((len("".join(buf)), lineno))
        i, n = 0, len(code)
        esc = False
        while i < n:
            ch = code[i]
            if esc:
                # backslash-newline (line continuation) drops both characters
                if ch == "\n":
                    pass
                else:
                    buf.append("\\" + ch)
                esc = False
                i += 1
                continue
            if ch == "\\" and not sq:
                if i == n - 1:
                    esc = True  # trailing backslash: continuation marker
                else:
                    buf.append("\\" + code[i + 1])
                    i += 1
                i += 1
                continue
            if ch == "'" and not dq:
                sq = not sq
            elif ch == '"' and not sq:
                dq = not dq
            buf.append(ch)
            i += 1
        if esc or sq or dq:
            if sq or dq:
                buf.append("\n")
            continue  # logical line continues on the next physical line
        emit()
    emit()
    return spans


def _extract_cmdsubs(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Blank out $( ... ) and `...` spans; return (blanked, [(offset, inner)]).

    Offsets are preserved (inner chars replaced by spaces) so line mapping
    stays valid. Single quotes disable substitution; double quotes do not.
    Nested substitutions are consumed here and re-extracted on recursion.
    """
    chars = list(text)
    spans: list[tuple[int, str]] = []
    i, n = 0, len(text)
    sq = dq = False
    while i < n:
        ch = text[i]
        if ch == "\\" and not sq:
            i += 2
            continue
        if ch == "'" and not dq:
            sq = not sq
            i += 1
            continue
        if ch == '"' and not sq:
            dq = not dq
            i += 1
            continue
        if sq:
            i += 1
            continue
        if ch == "`":
            j = i + 1
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 1
                j += 1
            inner = text[i + 1:j]
            for k in range(i, min(j + 1, n)):
                chars[k] = " "
            spans.append((i + 1, inner))
            i = j + 1
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            depth = 1
            j = i + 2
            isq = idq = False
            while j < n and depth:
                c2 = text[j]
                if c2 == "\\" and not isq:
                    j += 2
                    continue
                if c2 == "'" and not idq:
                    isq = not isq
                elif c2 == '"' and not isq:
                    idq = not idq
                elif not isq and not idq:
                    if c2 == "(":
                        depth += 1
                    elif c2 == ")":
                        depth -= 1
                j += 1
            inner = text[i + 2:j - 1]
            for k in range(i, j):
                chars[k] = " "
            spans.append((i + 2, inner))
            i = j
            continue
        i += 1
    return "".join(chars), spans


def _lineno_at(marks: list[tuple[int, int]], offset: int) -> int:
    idx = bisect.bisect_right([m[0] for m in marks], offset) - 1
    return marks[max(idx, 0)][1]


def _word_core(word: str) -> str:
    """Strip one matching pair of surrounding quotes."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word


def _is_sh_candidate(word: str) -> bool:
    core = _word_core(word)
    if "$(" in core or "`" in core or "*" in core:
        return False
    if not re.search(r"\.sh\}?$", core):
        return False
    return bool(re.match(r"^(\$|\.{1,2}/|/|~)", core))


def _is_seam_token(word: str) -> bool:
    for m in SEAM_DEFAULT_RE.finditer(word):
        var = m.group(1)
        if var in SEAM_VARS or var.endswith(SEAM_SUFFIXES):
            return True
    return False


def _scan_shell_level(text: str, marks: list[tuple[int, int]], base: int,
                      rel: str, waived: set[int], findings: list[Finding]) -> None:
    """Tokenize one command level; flag interpreter-less .sh command words."""
    blanked, subs = _extract_cmdsubs(text)
    for off, inner in subs:
        # Recursion re-uses the parent's mark table shifted by the span offset.
        inner_marks = [(mo + off, ln) for mo, ln in _remark(inner, marks, off)]
        _scan_shell_level(inner, inner_marks, 0, rel, waived, findings)

    i, n = 0, len(blanked)
    head = True          # next word is a command-head candidate
    decl = False         # inside local/export/... (rest are assignments/args)
    while i < n:
        ch = blanked[i]
        if ch in " \t\n":
            i += 1
            continue
        # separators — a new command context starts after them
        if blanked.startswith("&&", i) or blanked.startswith("||", i):
            head, decl = True, False
            i += 2
            continue
        if ch in ";|":
            head, decl = True, False
            i += 1
            continue
        if ch == "&":
            if i + 1 < n and blanked[i + 1] == ">":
                pass  # &> / &>> redirection: fall into word consumption
            elif i > 0 and blanked[i - 1] == ">":
                i += 1  # 2>&1-style: part of the redirection word
                continue
            else:
                head, decl = True, False
                i += 1
                continue
        if ch in "()":
            head, decl = True, False
            i += 1
            continue
        # consume one word (quotes already blanked-safe; keep quotes in text)
        start = i
        buf: list[str] = []
        while i < n:
            c = blanked[i]
            if c in " \t\n;|)":
                break
            if c == "(":
                # `arr2=(` keeps the paren in the word (array literal); any
                # other `(` is a subshell separator. The word ENDS at the
                # paren so the array consumer below takes over the elements.
                if ASSIGN_RE.match("".join(buf)):
                    buf.append(c)
                    i += 1
                break
            if c == "&" and not (buf and buf[-1].endswith(">")) \
               and not (i + 1 < n and blanked[i + 1] == ">"):
                break
            if c == "\\" and i + 1 < n:
                buf.append(blanked[i:i + 2])
                i += 2
                continue
            if c == "'":
                j = blanked.find("'", i + 1)
                j = n - 1 if j == -1 else j
                buf.append(blanked[i:j + 1])
                i = j + 1
                continue
            if c == '"':
                j = i + 1
                while j < n:
                    if blanked[j] == "\\":
                        j += 2
                        continue
                    if blanked[j] == '"':
                        break
                    j += 1
                buf.append(blanked[i:j + 1])
                i = min(j + 1, n)
                continue
            buf.append(c)
            i += 1
        word = "".join(buf)
        if not word:
            i += 1
            continue

        if ARRAY_ASSIGN_RE.match(word):
            # arr=( ... — consume elements as data until the matching `)`.
            depth = 1
            while i < n and depth:
                c = blanked[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                i += 1
            head = False
            continue
        if head and not decl:
            if REDIR_RE.match(word):
                continue
            if ASSIGN_RE.match(word):
                continue
            if word in DECL_WORDS:
                decl = True
                continue
            if word in LAUNCHERS or word in CONTROL_WORDS:
                continue
            # This word is the effective command.
            head = False
            if _is_sh_candidate(word) and not _is_seam_token(word):
                lineno = _lineno_at(marks, base + start)
                if lineno not in waived:
                    findings.append(Finding(
                        rel, lineno, "shell",
                        f"repo script in command position without an interpreter: "
                        f"{word} (name one — e.g. bash {word} — or waive with a "
                        f"reason: # {WAIVER_MARK}: <why this exec is intentional>)",
                    ))


def _remark(inner: str, parent_marks: list[tuple[int, int]], off: int):
    """Build a mark table for a command-substitution inner text."""
    marks = [(0, _lineno_at(parent_marks, off))]
    for pos, ch in enumerate(inner):
        if ch == "\n":
            marks.append((pos + 1, _lineno_at(parent_marks, off + pos + 1)))
    return marks


def check_shell_file(path: Path, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [Finding(rel, 0, "shell", f"unreadable file: {exc}")]

    data_lines = _heredoc_body_lines(lines)
    waived: set[int] = set()
    coded: list[tuple[int, str | None]] = []
    for lineno, raw in enumerate(lines, 1):
        if lineno in data_lines:
            coded.append((lineno, None))
            continue
        code, comment = _strip_comment(raw)
        if WAIVER_RE.search(comment):
            waived.add(lineno)
        coded.append((lineno, code))

    in_array = False
    for text, marks in _logical_spans(coded):
        first = text.strip()
        if in_array:
            if first.startswith(")"):
                in_array = False
            continue
        if re.search(r"=\(\s*$", text) and ")" not in text:
            in_array = True
            continue
        _scan_shell_level(text, marks, 0, rel, waived, findings)
    return findings


# ---------------------------------------------------------------------------
# Python subprocess check
# ---------------------------------------------------------------------------

def _sh_literal(node: ast.AST, names: dict[str, ast.AST], depth: int = 0) -> bool:
    """True when the expression statically resolves to a '*.sh' path."""
    if depth > 4:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.endswith(".sh")
    if isinstance(node, ast.JoinedStr):
        for part in reversed(node.values):
            if isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value:
                return part.value.endswith(".sh")
        return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sh_literal(node.right, names, depth + 1) or _sh_literal(node.left, names, depth + 1)
    if isinstance(node, ast.Name):
        target = names.get(node.id)
        return target is not None and _sh_literal(target, names, depth + 1)
    if isinstance(node, ast.Call):
        func = node.func
        dotted = ""
        if isinstance(func, ast.Attribute):
            parts = []
            cur: ast.AST = func
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            dotted = ".".join(reversed(parts))
        elif isinstance(func, ast.Name):
            dotted = func.id
        if dotted in {"os.path.join", "pathlib.Path", "Path", "str", "os.fspath"}:
            return any(_sh_literal(a, names, depth + 1) for a in node.args)
    return False


def _collect_names(body: list[ast.stmt], names: dict[str, ast.AST]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    names.setdefault(tgt.id, stmt.value)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None:
                names.setdefault(stmt.target.id, stmt.value)


def check_python_file(path: Path, rel: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Finding(rel, 0, "python", f"unreadable file: {exc}")]
    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError:
        return findings  # not our gate; py_compile sections own syntax
    lines = src.splitlines()

    imported_names: set[str] = set()
    has_subprocess_module = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    has_subprocess_module = True
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SUBPROCESS_FUNCS:
                    imported_names.add(alias.asname or alias.name)

    module_names: dict[str, ast.AST] = {}
    _collect_names(tree.body, module_names)

    # Pre-map each call to its innermost function scope for name resolution.
    func_scopes: list[tuple[int, int, dict[str, ast.AST]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope: dict[str, ast.AST] = {}
            _collect_names(node.body, scope)
            func_scopes.append((node.lineno, node.end_lineno or node.lineno, scope))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_call = False
        if isinstance(func, ast.Attribute) and func.attr in SUBPROCESS_FUNCS:
            if isinstance(func.value, ast.Name) and func.value.id == "subprocess" and has_subprocess_module:
                is_subprocess_call = True
        elif isinstance(func, ast.Name) and func.id in imported_names:
            is_subprocess_call = True
        if not is_subprocess_call or not node.args:
            continue
        argv = node.args[0]
        if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
            continue
        head = argv.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            if head.value in INTERPRETERS or not head.value.endswith(".sh"):
                continue
        names = dict(module_names)
        for lo, hi, scope in func_scopes:
            if lo <= node.lineno <= hi:
                names.update(scope)
        if not _sh_literal(head, names):
            continue
        lineno = node.lineno
        line_text = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        _, comment = _strip_comment(line_text)
        if WAIVER_RE.search(comment):
            continue
        findings.append(Finding(
            rel, lineno, "python",
            "subprocess argv head resolves to a repo .sh without an interpreter "
            "(use [\"bash\", <script>, ...]; or waive with a reason: "
            f"# {WAIVER_MARK}: <why this exec is intentional>)",
        ))
    return findings


# ---------------------------------------------------------------------------

def list_tracked_files(repo_root: Path) -> tuple[list[str], str | None]:
    """Tracked *.sh/*.py relative paths; falls back to a filesystem walk."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "*.sh", "*.py"],
            check=True, capture_output=True, text=True,
        )
        files = [ln for ln in out.stdout.splitlines() if ln.strip()]
        return files, None
    except (OSError, subprocess.CalledProcessError) as exc:
        fallback: list[str] = []
        for pat in ("*.sh", "*.py"):
            for p in sorted(repo_root.rglob(pat)):
                if ".git" in p.parts:
                    continue
                fallback.append(str(p.relative_to(repo_root)))
        return fallback, f"git ls-files unavailable ({exc}); filesystem walk used"


def run_check(repo_root: Path) -> tuple[list[Finding], int, str | None]:
    rels, note = list_tracked_files(repo_root)
    findings: list[Finding] = []
    scanned = 0
    for rel in sorted(set(rels)):
        p = repo_root / rel
        if not p.is_file():
            continue
        scanned += 1
        if rel.endswith(".sh"):
            findings.extend(check_shell_file(p, rel))
        elif rel.endswith(".py"):
            findings.extend(check_python_file(p, rel))
    findings.sort(key=lambda f: (f.path, f.line))
    return findings, scanned, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".", help="repository root (default: cwd)")
    args = ap.parse_args(argv)
    root = Path(args.repo_root).resolve()
    if not root.is_dir():
        print(f"error: repo root not a directory: {root}", file=sys.stderr)
        return 2
    findings, scanned, note = run_check(root)
    if note:
        print(f"note: {note}")
    for f in findings:
        print(f)
    if findings:
        print(f"INTERPRETER-CHECK: FAIL ({len(findings)} finding(s), {scanned} files scanned)")
        return 1
    print(f"INTERPRETER-CHECK: PASS ({scanned} files scanned, 0 findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
