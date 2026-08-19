#!/usr/bin/env python3
"""check-interp-exec — flag repo .sh scripts executed WITHOUT a named interpreter.

Root cause this guards against (#1160): on Termux/Android there is no
/bin/bash and no /usr/bin/env, so `"$DIR/foo.sh"` executed directly fails
silently (rc 126/127, swallowed by fail-open branches). Five defects shared
this root: #472, #663, #1151, #1157, #1159. The correct form is
`bash "$DIR/foo.sh"` (see lib/spawn-detached.sh).

What is flagged
  Shell: a token ending in `.sh` (quoted variable path or literal) in COMMAND
    position — start of a line/segment (after && || ; | & ( $( ` if then
    while until do !), optionally behind prefix launchers such as nohup,
    exec, setsid, env, sudo, command, time, timeout, nice.
  Python: subprocess.{run,Popen,call,check_call,check_output}([...]) whose
    first list element is a string literal ending in `.sh`, and
    os.{execv,execvp,execl,execlp,spawnv,spawnvp}("<literal>.sh", ...).

What is deliberately NOT flagged (false-positive guards)
  - Interpreter already named: bash "$x" / setsid bash "$x" / sh foo.sh
  - Sourcing: . "$f" / source "$f"
  - .sh used as an argument (grep, sed, cat, jq, cp, mv, tests like [ -r $f ])
    — these are never in command position, so the command-position rule
    excludes them structurally
  - Assignments (FOO="$x.sh"), array assignments (A=(a.sh b.sh)), and
    declarers (export/local/declare/readonly)
  - case patterns (pat1|pat2)) — tracked across lines via a case/esac stack
  - glob/regex tokens containing * ? [ or a backslash — a pattern is not an
    executable path
  - Operator/test seams: variables like CCC_SCAN_INJECTION_BIN hold an
    operator-supplied binary that need not be a bash script — the lint only
    matches tokens that LITERALLY end in .sh, so seam indirection passes
  - eval segments and lines carrying an inline waiver comment (same line or
    the comment line immediately above):
        interp-exec-ok: <reason>

Usage: check-interp-exec.py [--root DIR] [files...]
With no files, scans `git ls-files '*.sh' '*.py'` under --root (default .).
Exit 1 and print `path:line: <text>` per finding; exit 0 when clean.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

WAIVER = "interp-exec-ok:"
INTERPRETERS = {"bash", "sh", "zsh", "dash", "ash", "ksh", "mksh"}
LAUNCH_PREFIXES = {
    "nohup", "exec", "setsid", "env", "sudo", "command", "builtin",
    "time", "timeout", "nice", "ionice", "stdbuf", "chrt", "flock",
}
DECLARERS = {"export", "local", "declare", "readonly", "typeset", ".", "source"}
SEGMENT_KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "{"}

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
_ARRAY_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=\(")
_GLOB_CHARS = frozenset("*?[\\")
_PY_SUBPROC_RE = re.compile(
    r"subprocess\.(?:run|Popen|call|check_call|check_output)\(\s*\[\s*"
    r"[rubfRUBF]*(['\"])((?:(?!\1).)*\.sh)\1"
)
_PY_OS_EXEC_RE = re.compile(
    r"os\.(?:execv|execvp|execl|execlp|spawnv|spawnvp)\(\s*"
    r"[rubfRUBF]*(['\"])((?:(?!\1).)*\.sh)\1"
)
_HEREDOC_RE = re.compile(r"<<-?\s*\\?['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_CASE_START_RE = re.compile(r"(?:^\s*|[;|&({]\s*|\bthen\s+|\bdo\s+|\belse\s+)case\s")
_ESAC_RE = re.compile(r"esac\b")  # used with .match(line, p) after skip_ws
_ARRAY_OPEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=\(")


class _Lexer:
    """State machine for lex_segments — split out so each operator handler
    stays small enough for the repo's C901 gate (#348)."""

    def __init__(self, line: str):
        self.line = line
        self.n = len(line)
        self.i = 0
        self.segs: list[tuple[str, bool]] = []
        self.cur: list[str] = []
        self.quote = ""
        self.depth = 0      # $( nesting
        self.btick = False  # inside `...`
        self.cmd_next = True

    def flush(self, is_cmd: bool) -> None:
        self.segs.append(("".join(self.cur), is_cmd))
        self.cur = []

    def lex_quoted(self) -> None:
        c = self.line[self.i]
        if c == "\\" and self.quote == '"' and self.i + 1 < self.n:
            self.cur.append(self.line[self.i : self.i + 2])
            self.i += 2
            return
        # $( and ` are live command substitutions even inside "..."
        if self.quote == '"' and c == "$" and self.i + 1 < self.n and self.line[self.i + 1] == "(":
            self.flush(self.cmd_next)
            self.depth += 1
            self.cmd_next = True
            self.i += 2
            return
        if self.quote == '"' and c == "`":
            self.flush(self.cmd_next)
            self.btick = True
            self.cmd_next = True
            self.i += 1
            return
        self.cur.append(c)
        if c == self.quote:
            self.quote = ""
        self.i += 1

    def lex_backtick(self) -> None:
        if self.btick:
            self.flush(True)        # backtick contents were command position
            self.btick = False
            self.cmd_next = False   # resumes the surrounding token
        else:
            self.flush(self.cmd_next)
            self.btick = True
            self.cmd_next = True
        self.i += 1

    def lex_open_paren(self) -> None:
        if self.cur and "".join(self.cur).rstrip().endswith("="):
            self.cur.append("(")    # array assignment: =( ... )
            self.i += 1
            return
        self.flush(self.cmd_next)
        self.cmd_next = True        # subshell contents
        self.i += 1

    def lex_close_paren(self) -> None:
        self.flush(self.cmd_next if self.depth == 0 else True)
        if self.depth > 0:
            self.depth -= 1
            self.cmd_next = self.btick  # resumes outer token unless inside ``
        else:
            self.cmd_next = True        # case-body / post-subshell
        self.i += 1

    def lex_terminator(self, width: int) -> None:
        self.flush(self.cmd_next)
        self.cmd_next = True
        self.i += width


def lex_segments(line: str) -> list[tuple[str, bool]]:
    """Split a shell line into (text, is_command_start) segments.

    Quote-aware; splits on command terminators (&& || |& | ; &), subshell
    `(`, command substitutions `$(` and backticks. `$(`/`(`` contents are
    command position; text resuming after the closing `)`/backtick is a
    continuation of the surrounding token, NOT command position. `=(` is
    kept literal (array assignment). Backslash-newline continuations are
    joined by the caller before this runs.
    """
    st = _Lexer(line)
    while st.i < st.n:
        c = st.line[st.i]
        if st.quote:
            st.lex_quoted()
            continue
        if c in "\"'":
            st.quote = c
            st.cur.append(c)
            st.i += 1
            continue
        if c == "\\" and st.i + 1 < st.n:
            st.cur.append(st.line[st.i : st.i + 2])
            st.i += 2
            continue
        if c == "`":
            st.lex_backtick()
            continue
        if c == "$" and st.i + 1 < st.n and st.line[st.i + 1] == "(":
            st.flush(st.cmd_next)
            st.depth += 1
            st.cmd_next = True
            st.i += 2
            continue
        if c == "(":
            st.lex_open_paren()
            continue
        if c == ")":
            st.lex_close_paren()
            continue
        if c in ";&|":
            nxt = st.line[st.i + 1] if st.i + 1 < st.n else ""
            st.lex_terminator(2 if c != ";" and nxt in "&|" else 1)
            continue
        st.cur.append(c)
        st.i += 1
    st.flush(st.cmd_next)
    return st.segs



def tokenize(seg: str) -> list[str]:
    """Split a segment into whitespace-separated tokens, quote-aware."""
    toks: list[str] = []
    cur: list[str] = []
    quote = ""
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote:
            cur.append(c)
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "\"'":
            quote = c
            cur.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            cur.append(seg[i : i + 2])
            i += 2
            continue
        if c.isspace():
            if cur:
                toks.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        toks.append("".join(cur))
    return toks


# prefix options that consume the NEXT token as their argument
# (sudo -u ops, timeout -t 5, nice -n 5, flock -w 2, stdbuf -o L, ...)
_ARG_FLAGS = {"-u", "-g", "-p", "-h", "-t", "-k", "-n", "-o", "-w", "-e"}


def _strip_quotes(tok: str) -> str:
    return tok.replace('"', "").replace("'", "")


def _is_assignment(tok: str) -> bool:
    return bool(_ASSIGN_RE.match(tok))


def segment_command(seg: str) -> str | None:
    """Return the effective command token of a segment, or None to skip."""
    toks = tokenize(seg)
    if not toks:
        return None
    if toks[0].startswith("#"):
        return None
    # leading shell keywords / block openers
    while toks and toks[0] in SEGMENT_KEYWORDS:
        toks = toks[1:]
    if not toks:
        return None
    # declarers and sourcing: the rest is assignment-shaped or sourced
    if toks[0] in DECLARERS:
        return None
    # eval is too dynamic to judge statically
    if toks[0] == "eval":
        return None
    # array assignment: A=(a.sh b.sh) — the elements are data, not commands
    if _ARRAY_ASSIGN_RE.match(toks[0]):
        return None
    # leading VAR=val prefixes
    while toks and _is_assignment(toks[0]):
        toks = toks[1:]
    if not toks:
        return None
    # launcher prefixes, with their option arguments
    guard = 0
    while toks and toks[0] in LAUNCH_PREFIXES and guard < 8:
        guard += 1
        toks = toks[1:]
        # swallow this prefix's option-ish args (flags, KEY=val, bare numbers)
        while toks and (
            toks[0].startswith("-") or _is_assignment(toks[0]) or toks[0].isdigit()
        ):
            flag = toks[0]
            toks = toks[1:]
            if flag in _ARG_FLAGS and toks and not toks[0].startswith("-"):
                toks = toks[1:]  # the flag's own argument (sudo -u ops)
    if not toks:
        return None
    return toks[0]


def scan_shell_line(line: str) -> bool:
    """True if the line executes a literal .sh path without an interpreter."""
    if WAIVER in line:
        return False
    for seg, is_cmd in lex_segments(line):
        if not is_cmd:
            continue
        cmd = segment_command(seg)
        if cmd is None or cmd.startswith("#"):
            continue
        bare = _strip_quotes(cmd)
        if bare in INTERPRETERS:
            continue
        if _GLOB_CHARS & frozenset(bare):
            continue  # a glob/regex pattern is not an executable path
        if bare.endswith(".sh"):
            return True
    return False


_IN_RE = re.compile(r"\bin\b")


def _scan_pattern_end(line: str, pos: int) -> int:
    """Index of the case-pattern-terminating ')' outside quotes, or -1."""
    q = ""
    for j in range(pos, len(line)):
        c = line[j]
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        elif c == ")":
            return j
    return -1


def _scan_dsemi(line: str, pos: int) -> int:
    """Index of the body-terminating ';;' / ';&' / ';;&' outside quotes, or -1.

    A lone ';' is ordinary body syntax (`... ]; then`) and must NOT end the
    body — treating it as a terminator cascades the whole case block into
    mis-blanked "patterns".
    """
    q = ""
    n = len(line)
    j = pos
    while j < n:
        c = line[j]
        if q:
            if c == q:
                q = ""
        elif c in "\"'":
            q = c
        elif c == ";" and j + 1 < n and line[j + 1] in ";&":
            return j
        j += 1
    return -1


def _blank_case_regions(line: str, case_stack: list[str]) -> str:
    """Blank out case-pattern regions so their tokens are never judged.

    case_stack entries are "pre" (seen `case`, awaiting `in`), "pattern",
    or "body". Updated in place; state persists across lines of a file.
    """
    chars = list(line)
    pos = 0
    n = len(line)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            chars[k] = " "

    def skip_ws(p: int) -> int:
        while p < n and chars[p].isspace():
            p += 1
        return p

    guard = 0
    while guard < 32:
        guard += 1
        if not case_stack:
            m = _CASE_START_RE.search(line, pos)
            if not m:
                return "".join(chars)
            case_stack.append("pre")
            pos = m.end()
            continue
        top = case_stack[-1]
        if top == "pre":
            m = _IN_RE.search(line, pos)
            if not m:
                return "".join(chars)  # `in` on a later line; judge header normally
            case_stack[-1] = "pattern"
            pos = m.end()
            continue
        if top == "pattern":
            p = skip_ws(pos)
            if _ESAC_RE.match(line, p):
                case_stack.pop()
                pos = p + 4
                continue
            j = _scan_pattern_end(line, pos)
            blank(pos, j + 1 if j >= 0 else n)
            if j < 0:
                return "".join(chars)  # pattern continues on the next line
            case_stack[-1] = "body"
            pos = j + 1
            continue
        # body zone: ';;' / ';&' returns to the pattern zone, a nested `case`
        # opens a new block — whichever marker comes first wins.
        dsemi = _scan_dsemi(line, pos)
        m = _CASE_START_RE.search(line, pos)
        if m and (dsemi < 0 or m.start() < dsemi):
            case_stack.append("pre")
            pos = m.end()
            continue
        if dsemi >= 0:
            case_stack[-1] = "pattern"
            pos = dsemi + 2
            continue
        return "".join(chars)
    return "".join(chars)


def scan_shell(path: str, text: str) -> list[int]:
    findings: list[int] = []
    # join backslash-continuations so a wrapped command is judged as one line
    raw = text.splitlines()
    lines: list[tuple[int, str]] = []
    i = 0
    while i < len(raw):
        start = i
        buf = raw[i]
        while buf.endswith("\\") and not buf.endswith("\\\\") and i + 1 < len(raw):
            i += 1
            buf = buf[:-1] + " " + raw[i]
        lines.append((start + 1, buf))
        i += 1
    heredoc_end: str | None = None
    case_stack: list[str] = []
    in_array = False  # multi-line A=( ... ) — elements are data, not commands
    prev_waived = False  # previous line was an interp-exec-ok waiver comment
    for lineno, line in lines:
        waived = prev_waived
        prev_waived = WAIVER in line
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        m = _HEREDOC_RE.search(line)
        if m:
            heredoc_end = m.group(1)
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_array:
            if ")" in line:
                in_array = False
            continue
        am = _ARRAY_OPEN_RE.search(line)
        if am and ")" not in line[am.end():]:
            in_array = True
            continue
        if waived:
            continue
        judged = _blank_case_regions(line, case_stack)
        if scan_shell_line(judged):
            findings.append(lineno)
    return findings


def scan_python(path: str, text: str) -> list[int]:
    findings: list[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if WAIVER in line:
            continue
        s = line.strip()
        if s.startswith("#"):
            continue
        if _PY_SUBPROC_RE.search(line) or _PY_OS_EXEC_RE.search(line):
            findings.append(lineno)
    return findings


def repo_files(root: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.sh", "*.py"],
            cwd=root, capture_output=True, text=True, check=True,
        )
        return [f for f in out.stdout.splitlines() if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        # git unavailable: walk the tree
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules"}]
            for fn in filenames:
                if fn.endswith((".sh", ".py")):
                    found.append(os.path.relpath(os.path.join(dirpath, fn), root))
        return sorted(found)


def main(argv: list[str]) -> int:
    root = "."
    files: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
        else:
            files.append(argv[i])
            i += 1
    if not files:
        files = repo_files(root)
    total = 0
    for rel in files:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if rel.endswith(".sh"):
            hits = scan_shell(rel, text)
        elif rel.endswith(".py"):
            hits = scan_python(rel, text)
        else:
            continue
        src_lines = text.splitlines()
        for lineno in hits:
            src = src_lines[lineno - 1].strip() if lineno <= len(src_lines) else ""
            print(f"{rel}:{lineno}: {src}")
            total += 1
    if total:
        print(
            f"\ncheck-interp-exec: {total} repo .sh invocation(s) without a named interpreter.\n"
            "On Termux/Android there is no /bin/bash|/usr/bin/env — execute as "
            '`bash "$path"` instead (#1160).\n'
            "If a site is a deliberate operator seam, waive it inline: "
            "interp-exec-ok: <reason>",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
