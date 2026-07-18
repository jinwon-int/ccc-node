#!/usr/bin/env python3
"""PreToolUse guard — fail-closed enforcement of the "Fresh Approval Required" boundary.

Python reimplementation of the historical guard.sh (issue #452). The contract is
unchanged and is pinned by guard.test.sh, which invokes this guard as an
executable through the guard.sh shim:

  stdin  = PreToolUse hook payload JSON: {tool_name, tool_input:{command|file_path|url|query}}
  exit 0 = allow
  exit 2 = deny (harness aborts the tool call; stderr is shown to Claude)

Every denial also appends a metadata-only record (risk label + profile + tool,
never the raw command) to ~/.claude/state/approval-needed.log so blocked gated
actions surface as approval-needed.

Risk-profile model (see RISK-PROFILES.md):
  autonomous              — not matched here; proceeds silently.
  operator_notify         — proceeds; captured by the PostToolUse audit log.
  operator_approval_gated — DENIED until CCC_ALLOW_GATED=1 (operator approves).
  operator_review_gated   — DENIED; history/published-state change needing review.

Fail posture (matches the historical bash guard):
  - fail-OPEN only when stdin is empty/unparseable (the payload is unavailable);
  - every gate otherwise fails CLOSED; an unexpected internal error DENIES.

Why Python (issue #452): the bash guard hand-rolled shell tokenization across
several helpers and three derived string "views", and its own comments record
repeated parser-drift escapes. shlex gives real tokenization (quotes, `--`,
value-taking flags) so the host/target parsing needed for the managed-nodes
relaxation is sound rather than regex-approximated.
"""

import fnmatch
import json
import os
import re
import shlex
import stat
import sys
from datetime import datetime, timezone

# Tokens that, appearing anywhere in a systemd unit / pm2 process name, mark it a
# fleet service the node may control autonomously (operator-approved relaxation,
# issue #341/#436). ccc-telegram-bridge is included explicitly.
_FLEET_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:a2a|hermes|openclaw|broker|gateway|worker)(?:[^A-Za-z0-9]|$)"
    r"|ccc-telegram-bridge",
    re.IGNORECASE,
)

# systemctl/pm2 lifecycle verbs covered by the fleet relaxation (pure lifecycle;
# config-changing verbs like enable/disable/mask/daemon-* are NOT relaxed here).
_RELAX_VERBS = {
    "start", "restart", "reload", "stop", "kill",
    "try-restart", "reload-or-restart", "try-reload-or-restart", "force-reload",
}

# Verbs that make a systemctl/service/pm2 invocation lifecycle-sensitive.  The
# relaxed subset above may proceed for fleet units; configuration-changing
# verbs and pm2 delete remain gated.  Read-only verbs such as is-active/status/
# show are intentionally absent so they can accompany a restart verification.
_SYSTEMCTL_GATED_VERBS = _RELAX_VERBS | {
    "disable", "enable", "mask", "unmask", "daemon-reload", "daemon-reexec",
}
_PM2_GATED_VERBS = {"start", "restart", "reload", "stop", "delete", "kill"}

# ssh/scp option flags that consume the following token as their value. Getting
# this list right only ever makes the managed-node relaxation MORE precise;
# misclassifying leans fail-closed (the target is missed → not relaxed).
_SSH_VALUE_FLAGS = {
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}
_SCP_VALUE_FLAGS = {"-P", "-i", "-o", "-c", "-l", "-S", "-F", "-J", "-e", "--rsh", "--port"}

# External-node placement policy: internal/Family-Wiki resource fingerprints.
_EXTERNAL_RE = re.compile(
    r"(family[ _-]?wiki|가족위키|wiki-agent|[.]wiki-agent|seoyoon-family-wiki"
    r"|wiki[.]seoyoon-family[.]com|jinwon-int|hooks/cache/wiki[.]txt|wiki-candidates"
    r"|ccc-wiki-triage|wiki-record|wiki-log|CCC_WIKI_MEMORY_ENABLED|CCC_NODE_ISOLATION_PROFILE)",
    re.IGNORECASE,
)

FRESH_APPROVAL_NOTE = (
    "→ Fresh Approval Required (CLAUDE.md). NOTE: CCC_ALLOW_GATED=1 only works when set "
    "in the HARNESS process environment by the operator (e.g. relaunch the session with it, "
    "or the operator runs the approved command in their own shell). A CCC_ALLOW_GATED=1 prefix "
    "inside an agent Bash command has NO effect — this hook runs first, in its own environment. "
    "Agents: prefer a non-gated alternative path, or ask the operator to execute."
)


class Deny(Exception):
    def __init__(self, label, profile, detail):
        self.label = label
        self.profile = profile
        self.detail = detail


def _deny(label, profile, detail):
    raise Deny(label, profile, detail)


# --------------------------------------------------------------------------- #
# Managed-nodes allowlist (operator-owned; issue for managed-node writes).
# --------------------------------------------------------------------------- #
def _read_allowlist(env_var, basename):
    """Read an operator-owned allowlist file (may be empty).

    Path: $<env_var> or ~/.claude/<basename>. One bare token (host/unit/glob) per
    line; `#` comments and blank lines ignored; tokens with shell/whitespace
    metacharacters are rejected fail-closed. These files are agent-write-gated
    (see the operator-config gate), so they are the trusted boundary."""
    path = os.environ.get(env_var)
    if not path:
        home = os.environ.get("HOME") or "/root"
        path = os.path.join(home, ".claude", basename)
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line and re.fullmatch(r"[A-Za-z0-9_.*?\[\]@:/-]+", line):
                    entries.append(line)
    except OSError:
        pass
    return entries


def _managed_allowlist():
    """Allowlisted REMOTE host patterns — "these remote hosts are mine.\""""
    return _read_allowlist("CCC_MANAGED_NODES_ALLOW", "managed-nodes.allow")


def _managed_services_allowlist():
    """Allowlisted LOCAL service/unit/container/process names — "these local
    services are mine to control" (systemctl/service/pm2/docker/podman)."""
    return _read_allowlist("CCC_MANAGED_SERVICES_ALLOW", "managed-services.allow")


def _host_allowlisted(host, entries):
    host = host.strip()
    if not host:
        return False
    return any(fnmatch.fnmatch(host, e) for e in entries)


def _service_allowlisted(unit, entries):
    """Match a unit/container/process name, tolerating a trailing `.service` so an
    operator entry `myapp` matches both `myapp` and `myapp.service`."""
    unit = unit.strip()
    if not unit:
        return False
    cands = [unit]
    if unit.endswith(".service"):
        cands.append(unit[:-len(".service")])
    return any(fnmatch.fnmatch(c, e) for c in cands for e in entries)


def _strip_user(token):
    """`user@host` → `host`; leaves a bare host unchanged. Returns '' if empty."""
    return token.rsplit("@", 1)[-1] if "@" in token else token


def _ssh_target_host(toks, i):
    """toks[i] is ssh/sftp; return the target host token (user@ stripped) or None."""
    j = i + 1
    n = len(toks)
    while j < n:
        t = toks[j]
        if t == "--":
            j += 1
            break
        if t in _SSH_VALUE_FLAGS:
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    if j >= n:
        return None
    host = toks[j]
    if "://" in host:  # ssh://user@host/... form
        m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@]*@)?([^/:]+)", host)
        return m.group(1) if m else None
    host = _strip_user(host)
    return host or None


def _copy_spec_host(token):
    """Classify one scp/rsync argument token.

    Returns (host_or_None, is_remote_spec, parse_ok). A scp/rsync REMOTE target
    always contains ':' (host:path, user@host:path, or an rsync:// URL); a token
    with no such ':' is a local path. So detection keys on ':' and never misses a
    remote spec — a spec we cannot parse returns parse_ok=False (fail closed).
    """
    if token.startswith("-"):
        return None, False, True  # flag
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", token):  # rsync:// URL
        m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@]*@)?([^/:]+)", token)
        return (m.group(1), True, True) if m else (None, True, False)
    if token.startswith("/") or token.startswith("."):
        return None, False, True  # absolute / relative local path
    if ":" in token:
        m = re.match(r"^(?:([A-Za-z0-9_.-]+)@)?([A-Za-z0-9_.-]+):", token)
        return (m.group(2), True, True) if m else (None, True, False)
    return None, False, True  # bare local path


def _systemctl_remote_host(toks, i):
    """From a systemctl/service invocation at toks[i], find a -H/-M remote target.

    Returns (host_or_None, present). present=True with host=None means the flag
    appeared without a parseable value (fail closed downstream).
    """
    j = i + 1
    n = len(toks)
    while j < n:
        t = toks[j]
        if t in ("-H", "--host", "-M", "--machine"):
            if j + 1 < n:
                return _strip_user(toks[j + 1]) or None, True
            return None, True
        m = re.match(r"^(?:--host|--machine)=(.+)$", t)
        if m:
            return _strip_user(m.group(1)) or None, True
        j += 1
    return None, False


def _basename(tok):
    return tok.rsplit("/", 1)[-1]


def _scan_remote_hosts(toks):
    """Scan tokens for remote endpoints reached via ssh/scp/rsync/sftp/systemctl-H.

    Returns (hosts, saw_remote_tool, ok). ok=False means a remote tool was present
    but its target could not be parsed (caller must fail closed)."""
    hosts = set()
    saw = False
    i = 0
    n = len(toks)
    while i < n:
        b = _basename(toks[i])
        if b in ("ssh", "sftp"):
            saw = True
            host = _ssh_target_host(toks, i)
            if host is None:
                return hosts, saw, False
            hosts.add(host)
            break  # the remainder is the remote command, not local host specs
        if b in ("scp", "rsync"):
            saw = True
            for t in toks[i + 1:]:
                host, is_remote, ok = _copy_spec_host(t)
                if not ok:
                    return hosts, saw, False
                if is_remote and host:
                    hosts.add(host)
            break
        if b in ("systemctl", "service"):
            host, present = _systemctl_remote_host(toks, i)
            if present:
                saw = True
                if host is None:
                    return hosts, saw, False
                hosts.add(host)
        i += 1
    return hosts, saw, True


def stmt_remote_target_managed(stmt, entries):
    """True iff this statement's only remote reach is ssh/scp/rsync/sftp/systemctl-H
    to hosts that are ALL in the managed-nodes allowlist.

    Soundness: curl/wget/nc/ncat/ftp force False (the secret-exfil gate keeps full
    authority over those). Any unparseable target forces False. So a True result
    guarantees every remote endpoint the statement can reach is an owned node —
    which is what licenses skipping the blast-radius gates (secret deploy, remote
    rm, remote service/host lifecycle) for that statement.
    """
    if not entries:
        return False
    try:
        toks = shlex.split(stmt)
    except ValueError:
        return False
    if not toks:
        return False
    # Soundness guards — the relaxation must NOT apply when a statement can reach a
    # non-owned endpoint or run a local command hidden in an option/substitution:
    #   - an inherently-remote sender word (curl/wget/nc/ncat/ftp) ANYWHERE (incl.
    #     inside an -o value or a $()/`` substitution token) forces the full
    #     secret-exfil gate to keep authority;
    #   - ssh/scp `-o ProxyCommand/LocalCommand/PermitLocalCommand` values execute a
    #     LOCAL command, so a target host cannot vouch for them.
    for t in toks:
        if re.search(r"\b(curl|wget|nc|ncat|ftp)\b", t):
            return False
        if re.search(r"(?i)(proxy|local)command|permitlocalcommand", t):
            return False
    hosts, saw_remote_tool, ok = _scan_remote_hosts(toks)
    if not ok or not saw_remote_tool or not hosts:
        return False
    return all(_host_allowlisted(h, entries) for h in hosts)


# --------------------------------------------------------------------------- #
# git push parsing (force-push relaxation + tag-push detection).
# --------------------------------------------------------------------------- #
_GIT_GLOBAL_VALUE_FLAGS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
}


def _git_push_index(toks):
    """Index of the `push` subcommand tolerating global options (git -C x push …),
    or -1 if this is not a git-push command."""
    i = 0
    n = len(toks)
    while i < n and toks[i] != "git":
        i += 1
    if i >= n:
        return -1
    i += 1
    while i < n:
        t = toks[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
        elif t == "push":
            return i
        elif t.startswith("-"):
            i += 1
        else:
            return -1
    return -1


def _is_forcepush(cn, toks):
    if _git_push_index(toks) < 0:
        return False
    if re.search(r"(\s-[a-zA-Z]*f[a-zA-Z]*\b|--force-with-lease|--force(\s|=|$))", cn):
        return True
    if re.search(r"\s\+[A-Za-z0-9_./-]+:", cn):
        return True
    if re.search(r"\s\+[A-Za-z0-9_./-]+(\s|$)", cn):
        return True
    return False


def _forcepush_to_feature_branch(c, toks):
    """True = safe: a single explicit force-push to a clearly non-protected branch."""
    if re.search(r"[;&|`]|\$\(|\n", c):
        return False
    pi = _git_push_index(toks)
    if pi < 0:
        return False
    positionals = []
    j = pi + 1
    n = len(toks)
    while j < n:
        t = toks[j]
        if t in ("-o", "--push-option", "--repo", "--exec", "--receive-pack"):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        positionals.append(t)
        j += 1
    if len(positionals) != 2:
        return False
    refspec = positionals[1].lstrip("+")
    dst = refspec.rsplit(":", 1)[-1]
    for pre in ("refs/heads/", "heads/"):
        if dst.startswith(pre):
            dst = dst[len(pre):]
            break
    if not dst:
        return False
    if dst in ("main", "master", "develop", "HEAD", "@", "prod", "production", "stable"):
        return False
    if dst == "release" or dst.startswith(("release/", "release-", "releases/", "hotfix/")):
        return False
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", dst):
        return False
    return True


# --------------------------------------------------------------------------- #
# secret-exfil (egress of a credential file to a remote endpoint).
# --------------------------------------------------------------------------- #
def _secret_in(s):
    return re.search(r"(\.env([^A-Za-z0-9_.-]|$)|\.credentials|\bid_(rsa|dsa|ecdsa|ed25519)\b)", s) is not None


def _remote_in(s):
    return re.search(r"(^[A-Za-z][A-Za-z0-9+.-]*://|^([A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+:)", s) is not None


def _net_http_in(s):
    return re.search(r"\b(curl|wget|nc|ncat|ftp)\b", s) is not None


def _net_copy_in(s):
    return re.search(r"\b(scp|sftp|rsync)\b", s) is not None


def _net_any_in(s):
    return re.search(r"\b(curl|wget|nc|ncat|ftp|scp|sftp|rsync)\b", s) is not None


def _exfil_http_egress(t):
    """HTTP-ish sender (curl/wget/nc/...): egress unless every secret token is an
    output sink (`-o FILE`, `>FILE`) or itself a remote resource (a URL)."""
    for k, tok in enumerate(t):
        if not _secret_in(tok) or _remote_in(tok):
            continue
        prev = t[k - 1] if k > 0 else ""
        if re.fullmatch(r"(-o|--output|-O|--output-document)", prev):
            continue
        if re.fullmatch(r"[0-9]*>>?", prev):
            continue
        if re.match(r"^(--output|--output-document|-o)=", tok):
            continue
        if re.match(r"^[0-9]*>>?", tok):
            continue
        return True
    return False


def _exfil_copy_egress(t, rlast):
    """Copy tool (scp/rsync): egress = a LOCAL secret source before the remote dest."""
    for k, tok in enumerate(t):
        if not _secret_in(tok) or _remote_in(tok):
            continue
        if k < rlast:
            return True
    return False


def _exfil_stmt(stmt):
    """True = the statement egresses a credential file to a remote endpoint."""
    if not _secret_in(stmt) or not _net_any_in(stmt):
        return False
    stages = stmt.split("|")
    n = len(stages)

    # (1) piped read-then-send: a secret upstream feeding a net tool downstream.
    for i in range(n):
        if _secret_in(stages[i]) and any(_net_any_in(stages[j]) for j in range(i + 1, n)):
            return True

    # (2)/(3) direct: a net tool and a secret in the SAME stage.
    for stage in stages:
        if not _net_any_in(stage):
            continue
        try:
            t = shlex.split(stage)
        except ValueError:
            t = stage.split()
        rlast = max((k for k in range(len(t)) if _remote_in(t[k])), default=-1)
        if any(_net_http_in(tok) for tok in t) and _exfil_http_egress(t):
            return True
        if any(_net_copy_in(tok) for tok in t) and rlast >= 0 and _exfil_copy_egress(t, rlast):
            return True
    return False


# --------------------------------------------------------------------------- #
# statement splitting — QUOTE-AWARE, at top-level `;`, newline, `&&`, `||`, and
# `&` (not a single `|`,
# matching the bash guard). Quote-awareness is the reason for the shlex rewrite:
# an operator inside a quoted remote command (`ssh node "a && b"`) must stay
# within ONE statement so the whole remote command is judged against the ssh
# target, not torn apart and mis-read as a local op.
# --------------------------------------------------------------------------- #
def _split_statements_toplevel(c):
    stmts = []
    buf = []
    i = 0
    n = len(c)
    quote = None
    while i < n:
        ch = c[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(c[i + 1])
            i += 2
            continue
        if ch in (";", "\n"):
            stmts.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch == "&":
            stmts.append("".join(buf))
            buf = []
            i += 2 if (i + 1 < n and c[i + 1] == "&") else 1
            continue
        if ch == "|" and i + 1 < n and c[i + 1] == "|":
            stmts.append("".join(buf))
            buf = []
            i += 2
            continue
        buf.append(ch)
        i += 1
    stmts.append("".join(buf))
    return stmts


def _quote_strip(s):
    return s.replace('"', "").replace("'", "")


def _split_lifecycle_fragments(stmt):
    """Split a lifecycle statement into command-sized fragments.

    The top-level splitter deliberately preserves a quoted SSH remote body so
    every blast-radius gate can associate it with the remote target.  Service
    classification needs a narrower second view: each lifecycle invocation in
    that body must be judged independently, otherwise trailing read-only
    verification (``sleep; systemctl is-active/show``) is misread as restart
    targets.  Quote stripping is conservative here: separators inside a data
    string can only cause extra inspection and a fail-closed false positive,
    never hide a lifecycle invocation.
    """
    flattened = _quote_strip(stmt)
    return [
        fragment.strip()
        for fragment in re.split(r"(?:&&|\|\||[;&|\n{}()])+", flattened)
        if fragment.strip()
    ]


def _split_host_lifecycle_fragments(stmt):
    """Split quoted remote command bodies without unpacking data expressions.

    Host lifecycle needs command-separator splitting for mixed reboot/down
    detection, but unlike service parsing must preserve parentheses/braces so
    ``python -c 'os.system(\"reboot\")'`` cannot become a fake direct command.
    """
    flattened = _quote_strip(stmt)
    return [
        fragment.strip()
        for fragment in re.split(r"(?:&&|\|\||[;&|\n])+", flattened)
        if fragment.strip()
    ]


# --------------------------------------------------------------------------- #
# Operational-relax profile (operator-owned, fail-closed).
# --------------------------------------------------------------------------- #
_GUARD_PROFILE_PATH = "/etc/ccc-node/guard-profile"


def _operational_relax_enabled():
    """Whether the operator enabled the operational-relax profile on THIS node.

    Fail-closed: only a root-owned (uid 0), regular, non-symlink,
    non-group/world-writable ``/etc/ccc-node/guard-profile`` whose content
    carries the ``operational-relax`` token enables it. The agent runs
    unprivileged and cannot write a root-owned /etc file, so it cannot relax its
    own guard; any error, ambiguity, or weaker ownership fails closed to strict.

    ``CCC_GUARD_ASSUME_STRICT=1`` ignores the profile entirely. This is a
    STRICT-ONLY seam (it can only tighten, never relax), safe to expose to the
    environment: the guard test suite uses it so strict-semantics expectations
    hold on nodes where the operator has enabled operational-relax.

    Only the OPERATIONAL gates (service/container/orchestrator lifecycle and
    reboot) consult this. The catastrophic set — rm-catastrophic, secret-exfil,
    force-push/history-rewrite, DB destructive/migrate/replay, release/publish,
    repo-visibility, host power-down (poweroff/halt), and operator-config
    writes — is enforced regardless and never reads this profile.
    """
    if os.environ.get("CCC_GUARD_ASSUME_STRICT") == "1":
        return False                         # strict-only seam: can only tighten
    try:
        st = os.lstat(_GUARD_PROFILE_PATH)   # lstat: a symlink is not a regular file
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != 0:                       # must be operator (root)-owned
        return False
    if st.st_mode & 0o022:                   # group/world writable → untrusted
        return False
    try:
        with open(_GUARD_PROFILE_PATH, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return False
    for line in data.splitlines():
        if line.split("#", 1)[0].strip() == "operational-relax":
            return True
    return False


# --------------------------------------------------------------------------- #
# Quoted-heredoc data stripping (operator-approved FP fix).
# --------------------------------------------------------------------------- #
# A heredoc body is treated as pure DATA — and excluded from pattern gates —
# only when BOTH hold (anything else falls through unstripped, fail-closed):
#   1. the delimiter is quoted (<<'EOF' / <<"EOF"): the shell performs NO
#      parameter/command substitution inside the body, so nothing in the body
#      executes at parse time;
#   2. the command consuming stdin is a known pure data sink (cat / tee /
#      git commit): the body is written or recorded, never interpreted.
# Interpreter consumers (bash/sh/ssh/python/eval/...) keep their bodies
# scanned, as do unquoted heredocs (their $(...)/$VAR expand and can execute).
#
# "Pure data sink" is required to be PROVABLY TERMINAL (adversarial review on
# #571): after the heredoc intro on the same line only plain-file redirects may
# follow (no `| bash`, no `>(...)` process substitution, no `&&`-chained
# execution), and the terminator must be the LAST non-blank content of the
# whole command — otherwise a later statement in the same command could execute
# what the sink just wrote (`cat > s.sh <<'EOF' ... EOF` + `bash s.sh`).
# Only body LINES are removed: the sink command, its arguments and redirect
# targets, and both delimiter lines stay visible to every gate — so e.g.
# `tee /etc/ccc-node/guard-profile <<'EOF'` still trips the operator-config
# gate on the argument path.
_HEREDOC_INTRO_RE = re.compile(r"<<-?[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_.-]*)\1")
# Sink resolution must be IMMUTABLE AND PINNED (adversarial review on #571,
# round 4): bare command names resolve through mutable shell state — functions
# (incl. rc-file and exported/inherited ones), aliases, the hash table, and
# PATH — none of which command-text analysis can prove. An ABSOLUTE path
# invocation bypasses every one of those resolution channels, so only these
# exact pinned spellings qualify, and the binary at that path must itself be a
# root-owned, non-group/world-writable regular file at guard time.
_DATA_SINK_PATHS = {"/bin/cat", "/usr/bin/cat", "/bin/tee", "/usr/bin/tee"}
_GIT_SINK_PATHS = {"/bin/git", "/usr/bin/git"}
# After the intro, only redirection to a LITERAL file path may follow — no
# pipes, process substitutions, command separators, or expansions.
_HEREDOC_TAIL_RE = re.compile(r"^([ \t]*>{1,2}[ \t]*[A-Za-z0-9/._+:-]+)*[ \t]*$")
# After the terminator, only INERT trailing statements may follow (operator
# request: `cat > notes.md <<'EOF' ... EOF` + `echo saved` is a common data
# pattern): echo/printf/true/: with purely literal arguments — no command
# substitution, separators, redirects, or expansions, so a trailing statement
# provably cannot execute what the sink wrote. Anything else refuses stripping.
_INERT_TRAILING_RE = re.compile(
    r"^[ \t]*(?:(?:echo|printf|true|:)(?:[ \t]+[A-Za-z0-9/._+:%,'\"=-]+)*)?[ \t]*$"
)
# Sink names are trusted ONLY when nothing in the command (outside the body,
# which is inert data) can re-bind them (adversarial review on #571): a shell
# function or alias definition, or a loader/lookup env assignment (PATH,
# LD_PRELOAD, ...) can turn `cat` into an interpreter. Any such signal refuses
# stripping — fail closed on redefinition state we cannot prove.
_SINK_REDEFINITION_RE = re.compile(
    r"\b(?:cat|tee|git)[ \t]*\([ \t]*\)"            # cat() {...} function defs
    r"|\bfunction[ \t]+(?:cat|tee|git)\b"            # function cat {...}
    r"|\balias\b"                                     # any alias definition
    r"|\bhash\b"                                      # hash-table rebinding
    r"|\bexport[ \t]+-f\b"                            # function exporting
    r"|[<>]\("                                        # process substitution
    r"|\b(?:PATH|BASH_ENV|ENV|LD_PRELOAD|LD_LIBRARY_PATH|IFS)[ \t]*="
)


def _heredoc_sink_statement(prefix):
    """Whether the statement text introducing a heredoc is a pure data sink."""
    # The heredoc attaches to the last statement on its line.
    seg = re.split(r"[;&|]", prefix)[-1]
    try:
        words = shlex.split(seg)
    except ValueError:
        return False
    # The statement must START with an ABSOLUTE pinned sink path — no bare
    # names (mutable resolution), no env-assignment prefixes (FOO=... cat).
    if not words:
        return False
    head = words[0]
    if head in _DATA_SINK_PATHS:
        return _pinned_binary(head)
    if head in _GIT_SINK_PATHS and len(words) >= 2 and words[1] == "commit":
        return _pinned_binary(head)
    return False


def _pinned_binary(path):
    """The sink binary itself must be a root-owned, non-writable regular file."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not (st.st_mode & 0o022)


def _strip_quoted_heredoc_data(cmd):
    """Replace a qualifying quoted-heredoc body with a data marker.

    Fail-closed on every ambiguity: at most ONE quoted heredoc is stripped per
    command, and only when the sink is provably terminal (nothing but plain
    file redirects after the intro; nothing but blank lines after the
    terminator). Anything else returns the command unmodified so every gate
    scans the full text.
    """
    if "<<" not in cmd or "\n" not in cmd:
        return cmd
    # Inherited exported shell functions (BASH_FUNC_*) mean name resolution in
    # the child shell is not provable from the command text — refuse stripping
    # entirely (absolute-path sinks are immune, but keep the belt with the
    # suspenders; adversarial review on #571, round 4).
    if any(k.startswith("BASH_FUNC_") for k in os.environ):
        return cmd
    lines = cmd.split("\n")
    for i, line in enumerate(lines):
        intros = list(_HEREDOC_INTRO_RE.finditer(line))
        if not intros:
            continue
        # Exactly one quoted heredoc, fed by a pure data sink, with nothing but
        # literal-file redirects after the intro on the same line.
        if len(intros) != 1:
            return cmd
        m = intros[0]
        if not _heredoc_sink_statement(line[: m.start()]):
            return cmd
        if not _HEREDOC_TAIL_RE.match(line[m.end():]):
            return cmd
        delim = m.group(2)
        dash = line[m.start():m.start() + 3] == "<<-"
        end = None
        for j in range(i + 1, len(lines)):
            cand = lines[j].lstrip("\t") if dash else lines[j]
            if cand == delim:
                end = j
                break
        if end is None:
            return cmd  # unterminated — strip nothing
        # Provably terminal: after the terminator only blank lines or INERT
        # trailing statements (echo/printf/true/: with literal args) may
        # follow, so no later statement in this command can execute what the
        # sink wrote.
        if any(not _INERT_TRAILING_RE.match(rest) for rest in lines[end + 1:]):
            return cmd
        # The sink name must be provably UNSHADOWED: any function/alias/loader
        # redefinition signal OUTSIDE the body (the body itself is inert data)
        # refuses stripping. Checked against everything except the body lines.
        outside = "\n".join(lines[: i + 1] + lines[end:])
        if _SINK_REDEFINITION_RE.search(outside):
            return cmd
        out = lines[: i + 1]
        if end > i + 1:
            out.append("[CCC-HEREDOC-DATA]")
        out.extend(lines[end:])
        return "\n".join(out)
    return cmd


# --------------------------------------------------------------------------- #
# Main evaluation. Raises Deny to block; returns normally to allow.
# --------------------------------------------------------------------------- #
def evaluate(tool, cmd, fpath, tool_input_raw):
    # --- External-node privacy boundary (higher priority than escape hatch). ---
    # A root/operator placement policy, not a per-command gate: not bypassable
    # with CCC_ALLOW_GATED or command-local env assignments.
    if os.environ.get("CCC_NODE_ISOLATION_PROFILE", "fleet") == "external":
        probe = f"{tool} {cmd} {fpath} {tool_input_raw}"
        if _EXTERNAL_RE.search(probe):
            _deny("external-family-resource", "placement_policy",
                  "Family Wiki/internal resource access is disabled on this external node")
        low = tool.lower()
        if low.startswith("mcp__") and "wiki" in low:
            _deny("external-family-resource", "placement_policy",
                  "Wiki MCP tools are disabled on this external node")

    # --- Operator escape hatch: explicit, audited approval signal. ---
    if os.environ.get("CCC_ALLOW_GATED", "0") == "1":
        sys.stderr.write(
            f"ccc-node guard: CCC_ALLOW_GATED=1 set — gated action allowed by operator "
            f"(audit: tool={tool}).\n")
        return

    # --- Operator config files: agents may READ but never WRITE. ---
    # self-update.services/.repo bound the self-update blast radius; managed-nodes.allow
    # bounds the managed-node relaxation; managed-services.allow bounds the local-service
    # relaxation. All are operator-owned.
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        if (fpath.endswith("/self-update.services") or fpath.endswith("/self-update.repo")):
            _deny("self-update-config", "operator_approval_gated", f"{tool} on {fpath}")
        if fpath.endswith("/managed-nodes.allow"):
            _deny("managed-nodes-config", "operator_approval_gated", f"{tool} on {fpath}")
        if fpath.endswith("/managed-services.allow"):
            _deny("managed-services-config", "operator_approval_gated", f"{tool} on {fpath}")
        if fpath.endswith("/guard-profile"):
            _deny("guard-profile-config", "operator_approval_gated", f"{tool} on {fpath}")

    if tool != "Bash" or not cmd:
        return

    # Quoted-heredoc DATA bodies feeding pure sink commands are stripped before
    # pattern matching (operator-approved FP fix): a commit message or a notes
    # file that MENTIONS "rm -rf /" is data, not an execution path. Everything
    # else about the command (the sink itself, redirect targets, delimiters,
    # unquoted heredocs, interpreter-fed heredocs) keeps being scanned.
    c = _strip_quoted_heredoc_data(cmd)
    cn = _quote_strip(c)
    try:
        toks = shlex.split(cn)
    except ValueError:
        toks = cn.split()

    entries = _managed_allowlist()
    svc_entries = _managed_services_allowlist()
    statements_raw = _split_statements_toplevel(c)          # quotes preserved
    statements = [_quote_strip(st) for st in statements_raw]  # pattern-match view
    # A statement is "managed-remote" when its only remote reach is to owned nodes.
    # Judged on the QUOTED statement so a remote command stays intact.
    managed = [stmt_remote_target_managed(st, entries) for st in statements_raw]

    relax = _operational_relax_enabled()      # operator-owned operational-relax profile
    _gate_git(c, cn, toks)                    # force-push / history-rewrite
    _gate_broker_reconcile(c, cn)             # immutable absolute wrapper entrypoint
    _service_lifecycle(
        cn,
        statements,
        managed,
        svc_entries,
        compose_reconciliation=_safe_compose_reconciliation(c),
        relax=relax,
    )
    _gate_host_lifecycle(c, cn, statements, managed, relax=relax)
    _gate_operator_config(c, cn)              # self-update.* / managed-nodes.allow writes
    _gate_db(c)                               # destructive / migrate / replay
    _gate_release(c, cn, toks)                # publish / tag-push / repo visibility
    _gate_secret_exfil(c, statements, managed)
    _gate_rm(c, statements, managed)


def _gate_git(c, cn, toks):
    # Never relaxed by managed-nodes: published/shared-state changes stay review-gated.
    if _is_forcepush(cn, toks) and not _forcepush_to_feature_branch(c, toks):
        _deny("force-push", "operator_review_gated", c)
    if re.search(r"git\s+(filter-branch|filter-repo)(\s|$)|git-filter-repo", c):
        _deny("history-rewrite", "operator_review_gated", c)


def _host_lifecycle_cmd_kind(st):
    """Classify a DIRECT host-lifecycle command in the statement.

    Returns 'reboot' (recovers — the node comes back), 'down' (poweroff/halt/
    `shutdown` without `-r` — the node stays offline until manual power-on), or
    None when no such command WORD is a direct command (e.g. the token only
    appears as a grep pattern or inside a `python -c` string — those keep the
    fail-closed word gate). Keyed on token basenames via shlex so an
    interpreter-mediated `os.system("reboot")` is NOT read as a direct reboot."""
    try:
        toks = shlex.split(st)
    except ValueError:
        return None
    for idx, t in enumerate(toks):
        b = _basename(t)
        if b in ("poweroff", "halt"):
            return "down"
        if b == "reboot":
            return "reboot"
        if b == "shutdown":
            rest = toks[idx + 1:]
            if any(x == "--reboot" or re.fullmatch(r"-[a-zA-Z]*r[a-zA-Z]*", x) for x in rest):
                return "reboot"
            return "down"
    return None


def _stmt_has_remote_tool(st):
    try:
        toks = shlex.split(st)
    except ValueError:
        toks = st.split()
    for i, t in enumerate(toks):
        b = _basename(t)
        if b in ("ssh", "sftp", "scp", "rsync"):
            return True
        if b in ("systemctl", "service") and _systemctl_remote_host(toks, i)[1]:
            return True
    return False


def _gate_host_lifecycle(c, cn, statements, managed, relax=False):
    # reboot-class (recoverable) is autonomous on nodes you are entitled to — the
    # LOCAL node and managed remote nodes; an unlisted remote host, interpreter-
    # mediated forms, and the down-class (poweroff/halt/`shutdown` w/o -r, which
    # leave a node offline unattended) all stay gated everywhere.
    if _is_readonly_text_search(cn):
        return
    for st, mgd in zip(statements, managed):
        if not re.search(r"\b(shutdown|reboot|poweroff|halt)\b", st):
            continue
        # The top-level splitter keeps a quoted SSH body together so remote
        # ownership is judged once. Classify every command-sized fragment inside
        # that body: a recoverable reboot must never mask a later/earlier
        # poweroff, halt, or non-reboot shutdown.
        kinds = [
            kind
            for fragment in _split_host_lifecycle_fragments(st)
            if (kind := _host_lifecycle_cmd_kind(fragment)) is not None
        ]
        if "down" in kinds:
            _deny("host-lifecycle", "operator_approval_gated", c)
        if "reboot" in kinds and (relax or mgd or not _stmt_has_remote_tool(st)):
            continue  # every classified host lifecycle is recoverable reboot
        _deny("host-lifecycle", "operator_approval_gated", c)


def _gate_operator_config(c, cn):
    if re.search(r"self-update\.(services|repo)|managed-(nodes|services)\.allow"
                 r"|ccc-node/guard-profile", cn) \
            and not _is_readonly_config_command(cn):
        _deny("self-update-config", "operator_approval_gated", c)


def _gate_db(c):
    if re.search(r"\b(DROP\s+(TABLE|DATABASE)|TRUNCATE\s|FLUSHALL|FLUSHDB)\b", c, re.IGNORECASE):
        _deny("db-destructive", "operator_approval_gated", c)
    if re.search(
        r"\b((npm|pnpm|yarn|npx)(\s+run)?\s+db:migrate|make\s+db:migrate"
        r"|prisma\s+migrate\s+(deploy|dev)|alembic\s+(upgrade|downgrade)|knex\s+migrate)\b",
        c, re.IGNORECASE,
    ):
        _deny("db-migrate", "operator_approval_gated", c)
    if re.search(r"\b(broker|worker|gateway|hermes|a2a|nexus|openclaw)[A-Za-z0-9_-]*\s+replay(\s|$)", c):
        _deny("replay", "operator_approval_gated", c)


def _gate_release(c, cn, toks):
    if re.search(r"\b(npm|yarn|pnpm)\s+publish(\s|$)|gh\s+release\s+create(\s|$)", c):
        _deny("release/publish", "operator_review_gated", c)
    if _git_push_index(toks) >= 0 and re.search(r"\s--(tags|follow-tags)(\s|=|$)", cn):
        _deny("release/publish", "operator_review_gated", c)
    if re.search(r"gh\s+repo\s+edit(\s|$)[^|;&]*--visibility", c):
        _deny("repo-visibility", "operator_approval_gated", c)


def _gate_secret_exfil(c, statements, managed):
    # Relaxed only for a managed-remote statement (deploying config/keys to an owned node).
    for st, mgd in zip(statements, managed):
        if not st.strip():
            continue
        st_pub = re.sub(r"[A-Za-z0-9_./~-]*\.pub(\.pem)?", "PUBKEY", st)
        if _exfil_stmt(st_pub) and not mgd:
            _deny("secret-exfil", "operator_approval_gated", c)


_RM_RE = re.compile(
    r"\brm\b(\s+-[A-Za-z-]*)*\s+(/|~|\$HOME|\$\{HOME\}|/root|/etc|/var|/usr|/bin|/lib)([\s/*]|$)")


_RM_PRUNE_SAFE_FLAGS = {"-f", "--force", "-v", "--verbose"}
_RM_BAK_BASENAME_RE = re.compile(r"\.bak([-.][^/]*)?$")
# Operands must be LITERAL paths: no $/`` expansions (a variable can expand
# into extra, unrelated operands — adversarial review on #571), no tilde, no
# braces, no bracket globs, no whitespace-bearing quoting tricks.
_RM_LITERAL_OPERAND_RE = re.compile(r"[A-Za-z0-9/._+:*?-]+")


def _rm_is_backup_prune(st):
    """A non-recursive rm whose EVERY operand names a .bak backup artifact.

    Pruning stale timestamped backups (e.g. ``.env.bak-20260717-091410``)
    reduces on-disk secret sprawl; treating it as catastrophic forced operator
    intervention for routine hygiene (operator-approved relaxation, 2026-07-18).
    Fail-closed: a recursive flag, an unparseable statement, a glob in the
    DIRECTORY part, or any operand whose basename is not a ``.bak`` /
    ``.bak-<suffix>`` / ``.bak.<suffix>`` artifact all fall through to the
    deny. Originals (``.env``, keys, configs) are never matched by the
    basename rule, so only explicitly-named backup copies are prunable.
    """
    try:
        toks = shlex.split(st)
    except ValueError:
        return False
    if not toks or toks[0] != "rm":
        return False
    operands = []
    for t in toks[1:]:
        if t == "--":
            continue
        if t.startswith("-") and t != "-":
            if t not in _RM_PRUNE_SAFE_FLAGS:
                return False          # -r/-R/-d/--recursive/unknown stay gated
            continue
        operands.append(t)
    if not operands:
        return False
    for op in operands:
        if not _RM_LITERAL_OPERAND_RE.fullmatch(op):
            return False              # $VAR/`cmd`/~/{}/[] operands stay gated
        dirpart, _, base = op.rpartition("/")
        if any(ch in dirpart for ch in "*?"):
            return False              # globbing directories stays gated
        if not _RM_BAK_BASENAME_RE.search(base):
            return False
    return True


def _gate_rm(c, statements, managed):
    # Relaxed only when the rm is inside a managed-remote statement (the owned node
    # governs its own filesystem) or is an explicit .bak-artifact pruning; a local
    # catastrophic rm always denies.
    for st, mgd in zip(statements, managed):
        if not mgd and _RM_RE.search(st):
            if _rm_is_backup_prune(st):
                continue
            _deny("rm-catastrophic", "operator_approval_gated", c)


_LOOPBACK_HTTP_RE = re.compile(
    r"http://(?:localhost|127(?:[.]\d{1,3}){3}|\[::1\])(?::\d{1,5})?(?:/[^\s]*)?"
)
_LITERAL_PATH_RE = re.compile(r"/[A-Za-z0-9._/+:-]+")
_IMAGE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*")
_SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_BROKER_RECONCILE_PATH = "/usr/local/libexec/ccc-broker-reconcile"
_BROKER_RECONCILE_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])ccc-broker-reconcile(?![A-Za-z0-9_.-])"
)
_BROKER_ENV_OVERRIDES = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "COMPOSE_FILE",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_PROFILES",
    "COMPOSE_ENV_FILES",
)
_MAX_RUNBOOK_SLEEP_SECONDS = 300.0

# One whitelisted pre-reconcile companion in the broker Compose runbook:
# capturing the current git revision into an env var the compose file
# interpolates (label provenance). `git rev-parse HEAD` is side-effect-free
# (reads refs, prints a SHA — no hooks/aliases), and _split_safe_compose_sequence
# has already rejected any top-level `;`/`|`/`&&`/`&`/newline before this runs, so
# a full-match on the exact command leaves no room for a hidden substitution
# (e.g. `$(git rev-parse HEAD; rm -rf /)` splits at `;` and fails closed). This
# is the ONLY `$`-bearing statement the runbook accepts; every other `$` stays
# hard-denied by _literal_statement_tokens.
_REVISION_EXPORT_RE = re.compile(
    r"export[ \t]+A2A_BROKER_REVISION="
    r"(?:\$\([ \t]*git[ \t]+rev-parse(?:[ \t]+--short)?[ \t]+HEAD[ \t]*\)"
    r"|`[ \t]*git[ \t]+rev-parse(?:[ \t]+--short)?[ \t]+HEAD[ \t]*`)"
)


def _safe_revision_export(st):
    """Whether a statement is the whitelisted broker-revision env capture."""
    return bool(_REVISION_EXPORT_RE.fullmatch(st.strip()))


def _split_safe_compose_sequence(c):
    """Split the narrow Compose runbook grammar, rejecting unsafe controls.

    ``;``, newline, and ``&&`` are accepted sequencing controls. Pipes,
    backgrounding, and ``||`` are rejected. Quoted controls remain data (for
    example, an inspect format string or a loopback URL query).
    """
    stmts = []
    buf = []
    quote = None
    escaped = False
    i = 0
    while i < len(c):
        ch = c[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in (";", "\n"):
            stmts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        if ch == "&":
            if i + 1 < len(c) and c[i + 1] == "&":
                stmts.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            return None
        if ch == "|":
            return None
        buf.append(ch)
        i += 1
    if quote or escaped:
        return None
    stmts.append("".join(buf).strip())
    return [st for st in stmts if st]


def _literal_statement_tokens(st):
    """Tokenize one direct statement with no shell indirection/redirection."""
    if re.search(r"[`<>]|\$", st):
        return None
    try:
        toks = shlex.split(st)
    except ValueError:
        return None
    return toks or None


def _compose_up_detached(st, *, remote):
    """Return (is_compose, valid) for the direct detached-up grammar."""
    toks = _literal_statement_tokens(st)
    if not toks:
        return False, False
    if toks[0] == "docker":
        if len(toks) < 3 or toks[1] != "compose":
            return False, False
        args = toks[2:]
    elif toks[0] == "docker-compose":
        args = toks[1:]
    else:
        return False, False
    if not args or args[0] != "up":
        return True, False
    rest = args[1:]
    if not any(arg in ("-d", "--detach") for arg in rest):
        return True, False
    if not remote:
        return True, True

    # An unlisted remote host gets the same fleet-only relaxation as remote
    # systemd: require explicit fleet service names. Keep the remote grammar
    # intentionally small; the broker runbook only needs detached up and an
    # optional force-recreate/no-deps/build flag.
    allowed_flags = {
        "-d", "--detach", "--force-recreate", "--no-deps",
        "--remove-orphans", "--build", "--no-build", "--renew-anon-volumes",
    }
    services = []
    for arg in rest:
        if arg in allowed_flags:
            continue
        if arg.startswith("-"):
            return True, False
        if not _SERVICE_NAME_RE.fullmatch(arg):
            return True, False
        services.append(arg)
    return True, bool(services) and all(_FLEET_RE.search(s) for s in services)


def _safe_loopback_curl(toks):
    if not toks or toks[0] != "curl":
        return False
    urls = []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in ("--fail", "--silent", "--show-error"):
            i += 1
            continue
        if re.fullmatch(r"-[fsS]+", t):
            i += 1
            continue
        if t in ("--max-time", "--connect-timeout", "--retry"):
            if i + 1 >= len(toks) or not re.fullmatch(r"\d+(?:[.]\d+)?", toks[i + 1]):
                return False
            i += 2
            continue
        if t.startswith("-"):
            return False
        urls.append(t)
        i += 1
    return len(urls) == 1 and bool(_LOOPBACK_HTTP_RE.fullmatch(urls[0]))


def _safe_post_reconcile_companion(toks):
    """Read-only verification allowed after the reconciliation: docker inspect,
    loopback-only curl, and a bounded sleep."""
    if toks[:2] == ["docker", "inspect"] and len(toks) >= 3:
        return True
    if _safe_loopback_curl(toks):
        return True
    if toks[0] == "sleep" and len(toks) == 2 \
            and re.fullmatch(r"\d+(?:[.]\d+)?s?", toks[1]):
        return float(toks[1].removesuffix("s")) <= _MAX_RUNBOOK_SLEEP_SECONDS
    return False


def _safe_compose_sequence_body(c, *, remote):
    statements = _split_safe_compose_sequence(c)
    if not statements:
        return False
    compose_count = 0
    saw_cd = False
    saw_tag = False
    saw_revision_export = False
    post_reconcile = False
    for st in statements:
        if _safe_revision_export(st):
            # Whitelisted git-revision env capture — checked before the literal
            # tokenizer, which would otherwise reject its `$(...)` outright.
            if post_reconcile or saw_revision_export:
                return False
            saw_revision_export = True
            continue
        toks = _literal_statement_tokens(st)
        if not toks:
            return False
        is_compose, compose_ok = _compose_up_detached(st, remote=remote)
        if is_compose:
            if not compose_ok or post_reconcile or compose_count:
                return False
            compose_count += 1
            post_reconcile = True
            continue
        if toks[0] == "cd":
            if post_reconcile or saw_cd or len(toks) != 2 \
                    or not _LITERAL_PATH_RE.fullmatch(toks[1]):
                return False
            saw_cd = True
            continue
        if toks[:2] == ["docker", "tag"]:
            if post_reconcile or saw_tag or len(toks) != 4 \
                    or not all(_IMAGE_REF_RE.fullmatch(t) for t in toks[2:]):
                return False
            saw_tag = True
            continue
        if not post_reconcile or not _safe_post_reconcile_companion(toks):
            return False
    return compose_count == 1


def _safe_compose_reconciliation(c):
    """Allow one detached Compose reconciliation plus reviewed companions.

    Accepted local shape: optional literal ``cd`` and/or ``docker tag``, one
    direct ``compose up -d``, then read-only ``docker inspect``, bounded sleep,
    and loopback-only curl verification. The same body may be sent as one
    direct ``ssh host \"...\"`` command when every explicit Compose service is
    a fleet service. Everything else stays approval-gated.
    """
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        return False
    if _safe_compose_sequence_body(c, remote=False):
        return True
    if re.search(r"[`<>]|\$", c):
        return False
    try:
        toks = shlex.split(c)
    except ValueError:
        return False
    if len(toks) != 3 or toks[0] != "ssh" \
            or not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+", toks[1]):
        return False
    return _safe_compose_sequence_body(toks[2], remote=True)


def _safe_broker_reconcile(c):
    """Accept only the direct immutable broker-wrapper entrypoint.

    No assignment, shell/interpreter wrapper, compound command, alternate path,
    option-like service, or ambient daemon/Compose override is allowed. The
    installed wrapper independently rechecks its ownership and configuration.
    """
    if any(os.environ.get(name) is not None for name in _BROKER_ENV_OVERRIDES):
        return False
    if re.search(r"[;&|<>\n\r`$]", c):
        return False
    try:
        toks = shlex.split(c)
    except ValueError:
        return False
    return (
        len(toks) >= 2
        and toks[0] == _BROKER_RECONCILE_PATH
        and all(_SERVICE_NAME_RE.fullmatch(token) for token in toks[1:])
    )


def _gate_broker_reconcile(c, cn):
    if not _BROKER_RECONCILE_MENTION_RE.search(c):
        return
    if _is_readonly_text_search(cn):
        return
    if not _safe_broker_reconcile(c):
        _deny("broker-reconcile", "operator_approval_gated", c)


def _service_lifecycle(
    cn,
    statements,
    managed_stmt,
    svc_entries,
    *,
    compose_reconciliation,
    relax=False,
):
    # operational-relax (operator-owned profile): all service/container/
    # orchestrator lifecycle is autonomous on this node. The catastrophic set is
    # enforced by the other gates and never consults this flag.
    if relax:
        return
    # Precedence per statement: managed-remote (owned node) → fleet unit (#436) →
    # managed local service (operator-listed unit/container) → else deny.
    for st, mgd in zip(statements, managed_stmt):
        if mgd:
            continue  # owned remote node — governed by its own policy
        if re.search(r"\b(systemctl|service)\b[^;&|]*\b(start|restart|reload|stop|kill|disable|enable|mask|unmask|daemon-reload|daemon-reexec)\b", st):
            if not (_is_fleet_lifecycle(st) or _local_service_allowed(st, svc_entries)):
                _deny("service-lifecycle", "operator_approval_gated", cn)
        if re.search(r"\bpm2\b[^;&|]*\b(start|restart|reload|stop|delete|kill)\b", st):
            if not (_is_fleet_lifecycle(st) or _local_service_allowed(st, svc_entries)):
                _deny("service-lifecycle", "operator_approval_gated", cn)
        if re.search(r"\b(docker|podman)\b[^;&|]*\b(run|up|start|restart|stop|kill|rm|pause|unpause|down)\b", st):
            if not (
                compose_reconciliation
                or _local_service_allowed(st, svc_entries)
            ):
                _deny("service-lifecycle", "operator_approval_gated", cn)
        if re.search(r"\bkubectl\b[^;&|]*\b(rollout\s+restart|scale|delete|drain|cordon|uncordon)\b", st):
            _deny("service-lifecycle", "operator_approval_gated", cn)
        if re.search(r"(^|[\s;|&])(restart-worker|stop-broker)([\s;|&]|$)", st):
            _deny("service-lifecycle", "operator_approval_gated", cn)


def _local_service_allowed(st, svc_entries):
    """True iff every unit/container/process a systemctl/service/pm2/docker/podman
    lifecycle command in the statement targets is in the operator-owned
    managed-services allowlist (and ≥1 target). Any un-parseable target → False
    (fail closed), so an unlisted or ambiguous target keeps the gate."""
    if not svc_entries:
        return False
    units, ok = _lifecycle_units(st)
    if not ok or not units:
        return False
    return all(_service_allowlisted(u, svc_entries) for u in units)


_DOCKER_LIFECYCLE_VERBS = {
    "run", "up", "start", "restart", "stop", "kill", "rm", "pause", "unpause", "down",
}


def _lifecycle_units(st):
    """Collect the target names of every systemctl/service/pm2/docker/podman
    lifecycle command in the statement. Returns (units, ok); ok=False if any such
    command's targets cannot be cleanly parsed (targetless `daemon-reload`, a
    docker global flag / `compose` / `$()` — all uncertain → caller fails closed)."""
    try:
        toks = shlex.split(st)
    except ValueError:
        return [], False
    units = []
    i = 0
    n = len(toks)
    while i < n:
        b = _basename(toks[i])
        if b in ("systemctl", "service", "pm2", "docker", "podman"):
            got, i, ok = _extract_unit_targets(b, toks, i, units)
            if not ok:
                return units, False
            if not got:
                # a lifecycle keyword with no clean target (e.g. daemon-reload) —
                # only uncertain if it WAS a gated verb; otherwise (status/ps) skip.
                pass
            continue
        i += 1
    return units, True


def _extract_unit_targets(tool, toks, i, units):
    """Parse one systemctl/service/pm2/docker/podman command starting at toks[i].
    Appends target names to `units`. Returns (found_target, next_i, ok); ok=False
    marks an un-parseable lifecycle target so the caller fails closed."""
    if tool == "service":
        if i + 1 < len(toks) and not toks[i + 1].startswith("-"):
            units.append(toks[i + 1])
            return True, i + 2, True
        return False, i + 1, False
    if tool in ("docker", "podman"):
        return _docker_targets(toks, i, units)
    return _systemctl_pm2_targets(tool, toks, i, units)


def _docker_targets(toks, i, units):
    n = len(toks)
    nxt = toks[i + 1] if i + 1 < n else None
    if nxt is None or nxt.startswith("-") or nxt == "compose":
        return False, i + 1, False  # global flag (incl -H remote) / compose → uncertain
    if nxt not in _DOCKER_LIFECYCLE_VERBS:
        return False, i + 2, True   # non-lifecycle subcommand (ps/images/…) — ignore
    j = i + 2
    got = False
    while j < n:
        t = toks[j]
        if t in ("|", ";", "&"):
            break
        if t.startswith("-") or "$(" in t or "`" in t:
            return got, j, False    # a flag/substitution on docker lifecycle → uncertain
        units.append(t)
        got = True
        j += 1
    return got, j, got


def _systemctl_pm2_targets(tool, toks, i, units):
    n = len(toks)
    j = i + 1
    while j < n:
        t = toks[j]
        if tool == "systemctl" and t in ("-H", "--host", "-M", "--machine"):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    if j >= n:
        return False, j, False
    j += 1  # skip the verb
    got = False
    while j < n:
        t = toks[j]
        if t in ("|", ";", "&"):
            break
        if t in ("-s", "--signal", "--kill-who", "--kill-whom"):
            j += 2
            continue
        if t.startswith("#") or re.match(r"^[0-9]*(>>?|<)", t):
            break
        if t.startswith("-"):
            j += 1
            continue
        units.append(t)
        got = True
        j += 1
    return got, j, got


def _is_fleet_lifecycle(stmt):
    """True = every systemctl/service/pm2 lifecycle invocation in the statement
    targets fleet units with a relaxed (pure lifecycle) verb (#436).

    Returns False if the statement contains no systemctl/service/pm2 lifecycle at
    all (so docker/kubectl/bareword denials are unaffected by this check).

    Quoted SSH bodies and shell-function bodies are inspected one command at a
    time.  Read-only service checks do not invalidate an otherwise relaxed
    restart, but a non-fleet target or config-changing verb still fails closed.
    """
    saw = False
    for fragment in _split_lifecycle_fragments(stmt):
        try:
            toks = shlex.split(fragment)
        except ValueError:
            return False
        i = 0
        n = len(toks)
        while i < n:
            b = _basename(toks[i])
            if b == "systemctl":
                verb, _verb_i = _systemctl_verb(toks, i)
                if verb in _SYSTEMCTL_GATED_VERBS:
                    saw = True
                    if not _systemctl_fleet(toks, i):
                        return False
            elif b == "service":
                verb = toks[i + 2] if i + 2 < n else None
                if verb in _SYSTEMCTL_GATED_VERBS:
                    saw = True
                    if not _service_fleet(toks, i):
                        return False
            elif b == "pm2":
                verb, _verb_i = _pm2_verb(toks, i)
                if verb in _PM2_GATED_VERBS:
                    saw = True
                    if not _pm2_fleet(toks, i):
                        return False
            i += 1
    return saw


def _targets_all_fleet(target_toks):
    """target_toks = candidate unit tokens; True iff ≥1 present and all fleet.
    Flags are skipped; -s/--signal (kill) consume a value; a `#` starts a comment;
    a redirection (>f, 2>&1, >) is skipped."""
    found = False
    skip = False
    for t in target_toks:
        if skip:
            skip = False
            continue
        if t.startswith("#"):
            break
        if re.match(r"^[0-9]*(>>?|<)", t):
            if re.fullmatch(r"[0-9]*(>>?|<)", t):
                skip = True
            continue
        if t in ("-s", "--signal", "--kill-who", "--kill-whom"):
            skip = True
            continue
        if t.startswith("-"):
            continue
        found = True
        if not _FLEET_RE.search(t):
            return False
    return found


def _systemctl_verb(toks, i):
    n = len(toks)
    j = i + 1
    while j < n:
        t = toks[j]
        if t in ("-H", "--host", "-M", "--machine"):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        return t, j
    return None, None


def _systemctl_fleet(toks, i):
    verb, verb_i = _systemctl_verb(toks, i)
    if verb not in _RELAX_VERBS:
        return False
    return _targets_all_fleet(toks[verb_i + 1:])


def _service_fleet(toks, i):
    # SysV order: service <unit> <verb>
    if i + 2 >= len(toks):
        return False
    unit = toks[i + 1]
    verb = toks[i + 2]
    if verb not in _RELAX_VERBS:
        return False
    return _FLEET_RE.search(unit) is not None


def _pm2_verb(toks, i):
    n = len(toks)
    j = i + 1
    while j < n:
        t = toks[j]
        if t.startswith("-"):
            j += 1
            continue
        return t, j
    return None, None


def _pm2_fleet(toks, i):
    verb, verb_i = _pm2_verb(toks, i)
    if verb not in ("start", "restart", "reload", "stop", "kill"):
        return False
    return _targets_all_fleet(toks[verb_i + 1:])


def _is_readonly_text_search(cn):
    return re.fullmatch(r"\s*(grep|rg)(\s+[^;&|<>$`()]+)+\s*", cn) is not None


def _is_readonly_config_command(cn):
    return re.fullmatch(
        r"\s*(cat|grep|stat|test|wc|sha256sum)(\s+-[A-Za-z0-9_-]*)*\s+"
        r"[^;&|<>$`()]*(self-update\.(services|repo)|managed-(nodes|services)\.allow)"
        r"([\s]+[^;&|<>$`()]+)*\s*",
        cn,
    ) is not None


def _write_approval_log(label, profile, tool):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = os.environ.get("CCC_APPROVAL_LOG")
    if not log:
        home = os.environ.get("HOME") or "/root"
        log = os.path.join(home, ".claude", "state", "approval-needed.log")
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\tDENY[{label}]\tprofile={profile}\ttool={tool or '?'}\n")
    except OSError:
        pass


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    if not raw.strip():
        return 0  # fail-open: payload unavailable
    try:
        payload = json.loads(raw)
    except Exception:
        return 0  # fail-open: payload unparseable
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    cmd = ti.get("command") or "" if isinstance(ti, dict) else ""
    fpath = ti.get("file_path") or "" if isinstance(ti, dict) else ""
    try:
        tool_input_raw = json.dumps(ti, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        tool_input_raw = ""

    try:
        evaluate(tool, cmd, fpath, tool_input_raw)
    except Deny as d:
        _write_approval_log(d.label, d.profile, tool)
        sys.stderr.write(f"BLOCKED by ccc-node guard [{d.label}] (profile={d.profile}): {d.detail}\n")
        sys.stderr.write(FRESH_APPROVAL_NOTE + "\n")
        return 2
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on unexpected internal error
        _write_approval_log("guard-internal-error", "operator_approval_gated", tool)
        sys.stderr.write(f"BLOCKED by ccc-node guard [guard-internal-error]: {exc}\n")
        sys.stderr.write(FRESH_APPROVAL_NOTE + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
