#!/usr/bin/env python3
"""Re-pin registered Codex web MCP launches from ``npx -y`` to the global install.

Codex counterpart of ``claude/mcp-setup.sh --web-launch-only`` (#2300, #2315;
this lane #2543). ``npx -y <pkg>`` leaves a resident ``npm exec`` wrapper per
server per session and fetches the latest npm release every launch, so a
server that this harness registered as::

    [mcp_servers.<name>]
    command = "npx"
    args = ["-y", "<pkg>[@ver]"]

is rewritten to ``command = "node"`` / ``args = ["<abs cli>"]`` when (and only
when) the package is installed globally. Contract:

* Only ``searxng``, ``firecrawl`` and ``context7`` are considered, and only in
  exactly the shape above. Custom launches, other servers and every other key
  (``env`` tables included) are left byte-for-byte; no server is ever added
  and nothing is installed.
* The CLI path follows ``global_cli()`` in ``claude/mcp-setup.sh``: the first
  ``bin`` entry of ``<npm root -g>/<pkg>/package.json``, which must exist.
* The npm root is the runtime user's: ``npm root -g`` runs with the config
  owner's HOME (a node whose runtime user has its own prefix, such as
  ``~/.local/lib/node_modules``, is not read from root's prefix — ND-4262),
  then ``<owner home>/.local/lib/node_modules`` as a fallback. ``--npm-root``
  overrides both.
* Edits are line-level (just the two lines per block). The result is
  re-parsed with ``tomllib`` and must equal the original document with only
  those two keys changed, before and after the write; otherwise nothing is
  written, or the original bytes are restored. A backup is written to
  ``<backup-dir>/codex-config.toml.before-mcp-pin-<date>`` (mode 0600) first.
* Config contents and env values are never printed. ``--dry-run`` reports
  only the names of servers that would change.

Exit status: 0 done (including nothing to do / skipped), 1 failure (config
unsafe/unreadable/unparsable, write or verification failed — original kept),
2 usage error.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import functools
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Callable, Union

try:
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10 fleet nodes
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


CONFIG_NAME = "config.toml"
BACKUP_PREFIX = "codex-config.toml.before-mcp-pin-"
MAX_CONFIG_BYTES = 1024 * 1024
NPM_TIMEOUT_S = 30

# Server name -> npm package, the same set and packages as mcp-setup.sh's
# --web-launch-only (web_relaunch searxng/context7/firecrawl).
WEB_SERVERS: dict[str, str] = {
    "searxng": "mcp-searxng",
    "firecrawl": "firecrawl-mcp",
    "context7": "@upstash/context7-mcp",
}

_ANY_TABLE_RE = re.compile(r"^\s*\[\[?[^\r\n]+\]\]?\s*(?:#.*)?$")
_COMMAND_RE = re.compile(
    r"""^(?P<indent>[ \t]*)command[ \t]*=[ \t]*(?:"npx"|'npx')(?P<suffix>[ \t]*(?:#.*)?)$"""
)
_ARGS_RE = re.compile(
    r"^(?P<indent>[ \t]*)args[ \t]*=[ \t]*\[[^\]\r\n#]*\](?P<suffix>[ \t]*(?:#.*)?)$"
)


class RefreshError(RuntimeError):
    """A failure that leaves the config untouched (message is content-free)."""


def _header_re(name: str) -> re.Pattern[str]:
    quoted = re.escape(name)
    return re.compile(
        rf"""^[ \t]*\[[ \t]*mcp_servers[ \t]*\.[ \t]*(?:{quoted}|"{quoted}"|'{quoted}')[ \t]*\][ \t]*(?:#.*)?$"""
    )


def _parse(text: str) -> dict:
    if _toml is None:
        raise RefreshError("no TOML parser (tomllib/tomli) available")
    return _toml.loads(text)


def _toml_string(value: str) -> str:
    """A TOML basic string for ``value`` (escapes quote, backslash, controls)."""
    out = []
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _render_command_line(indent: str, suffix: str) -> str:
    return f'{indent}command = "node"{suffix}'


def _render_args_line(indent: str, cli: str, suffix: str) -> str:
    return f"{indent}args = [{_toml_string(cli)}]{suffix}"


# --- npm root / CLI resolution ------------------------------------------------


def _home_of_uid(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_dir or None
    except (KeyError, OSError):
        return None


def runtime_home(config_path: Path) -> str:
    """Home of the config's owner (the runtime user); else the caller's HOME."""
    try:
        owner = config_path.lstat().st_uid
    except OSError:
        owner = None
    if owner is not None and owner != os.geteuid():
        home = _home_of_uid(owner)
        if home:
            return home
    return os.environ.get("HOME") or _home_of_uid(os.geteuid()) or "/"


def _npm_env(home: str) -> dict[str, str]:
    env = dict(os.environ)
    if os.path.realpath(home) != os.path.realpath(env.get("HOME", "") or "/"):
        # Resolving for another account: its own ~/.npmrc decides the prefix,
        # not npm settings inherited from the caller.
        for key in list(env):
            if key.lower().startswith("npm_config_"):
                del env[key]
        env["HOME"] = home
    return env


def npm_global_roots(home: str) -> list[str]:
    """Candidate global node_modules roots for the runtime user, in order."""
    roots: list[str] = []
    env = _npm_env(home)
    npm = shutil.which("npm", path=env.get("PATH"))
    if npm:
        try:
            proc = subprocess.run(
                [npm, "root", "-g"],
                env=env,
                cwd=home if os.path.isdir(home) else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=NPM_TIMEOUT_S,
                check=False,
            )
            first = (proc.stdout or "").strip().splitlines()
            if proc.returncode == 0 and first and first[0].strip():
                roots.append(first[0].strip())
        except (OSError, subprocess.SubprocessError):
            pass
    fallback = os.path.join(home, ".local", "lib", "node_modules")
    if os.path.isdir(fallback) and fallback not in roots:
        roots.append(fallback)
    return roots


def global_cli(root: str, pkg: str) -> str | None:
    """Mirror of mcp-setup.sh global_cli(): first ``bin`` of the package.

    ``path.resolve(<root>/<pkg>, rel)``; the file must exist (``[ -f ]``).
    """
    pkg_dir = os.path.join(root, pkg)
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    binfield = meta.get("bin")
    if isinstance(binfield, str):
        rel = binfield
    elif isinstance(binfield, dict) and binfield:
        rel = next(iter(binfield.values()))
    else:
        return None
    if not isinstance(rel, str) or not rel:
        return None
    cli = os.path.abspath(os.path.join(pkg_dir, rel))
    return cli if os.path.isfile(cli) else None


def resolve_cli(roots: list[str], pkg: str) -> str | None:
    for root in roots:
        cli = global_cli(root, pkg)
        if cli:
            return cli
    return None


# --- planning -----------------------------------------------------------------


RootsSource = Union[list[str], Callable[[], list[str]]]


def _is_harness_npx(entry: object, pkg: str) -> bool:
    if not isinstance(entry, dict) or entry.get("command") != "npx":
        return False
    args = entry.get("args")
    if not isinstance(args, list) or len(args) != 2 or args[0] != "-y":
        return False
    spec = args[1]
    if not isinstance(spec, str):
        return False
    if spec == pkg:
        return True
    return spec.startswith(pkg + "@") and len(spec) > len(pkg) + 1


def _block_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    header = _header_re(name)
    starts = [i for i, line in enumerate(lines) if header.match(line.rstrip("\r\n"))]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ANY_TABLE_RE.match(lines[j].rstrip("\r\n")):
            end = j
            break
    return start, end


def _split_eol(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def _edit_block(lines: list[str], name: str, cli: str) -> bool:
    """Rewrite the two launch lines of ``[mcp_servers.<name>]`` in place.

    Only a plain table with exactly one single-line ``command = "npx"`` and one
    single-line ``args = [...]`` is edited; any other layout returns False.
    """
    bounds = _block_bounds(lines, name)
    if bounds is None:
        return False
    cmd_idx: list[int] = []
    args_idx: list[int] = []
    for k in range(bounds[0] + 1, bounds[1]):
        body = lines[k].rstrip("\r\n")
        if _COMMAND_RE.match(body):
            cmd_idx.append(k)
        elif _ARGS_RE.match(body):
            args_idx.append(k)
    if len(cmd_idx) != 1 or len(args_idx) != 1:
        return False
    body, eol = _split_eol(lines[cmd_idx[0]])
    m = _COMMAND_RE.match(body)
    assert m is not None
    lines[cmd_idx[0]] = _render_command_line(m["indent"], m["suffix"]) + eol
    body, eol = _split_eol(lines[args_idx[0]])
    m = _ARGS_RE.match(body)
    assert m is not None
    lines[args_idx[0]] = _render_args_line(m["indent"], cli, m["suffix"]) + eol
    return True


def _classify(name: str, entry: object, pkg: str, roots: RootsSource) -> tuple[str | None, str]:
    """(cli, note): cli is set only for a harness-shaped npx launch with a global install."""
    if not _is_harness_npx(entry, pkg):
        if isinstance(entry, dict) and entry.get("command") == "node":
            return None, f"{name}: already node (unchanged)"
        return None, f"{name}: custom launch — left untouched"
    cli = resolve_cli(roots() if callable(roots) else roots, pkg)
    if not cli:
        return None, (
            f"{name}: SKIPPED — {pkg} is not installed globally "
            f"(npm install -g {pkg}, then re-run)"
        )
    return cli, ""


def plan(text: str, roots: RootsSource) -> tuple[str, list[tuple[str, str]], list[str]]:
    """Return (new_text, [(server, cli)], notes). Raises RefreshError.

    ``roots`` may be a callable so npm is only spawned when some server is a
    harness-shaped npx launch.
    """
    try:
        data = _parse(text)
    except RefreshError:
        raise
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        raise RefreshError("config.toml does not parse as TOML — left untouched") from exc
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    lines = text.splitlines(keepends=True)
    changes: list[tuple[str, str]] = []
    notes: list[str] = []
    for name, pkg in WEB_SERVERS.items():
        if name not in servers:
            continue  # not registered: never added
        cli, note = _classify(name, servers[name], pkg, roots)
        if cli is None:
            notes.append(note)
        elif _edit_block(lines, name, cli):
            changes.append((name, cli))
        else:
            notes.append(
                f"{name}: layout not recognized (not a plain [mcp_servers.{name}] table) — left untouched"
            )
    new_text = "".join(lines)
    if changes:
        expected = copy.deepcopy(data)
        for name, cli in changes:
            expected["mcp_servers"][name]["command"] = "node"
            expected["mcp_servers"][name]["args"] = [cli]
        verify(new_text, expected)
    return new_text, changes, notes


def verify(text: str, expected: dict) -> None:
    try:
        parsed = _parse(text)
    except Exception as exc:
        raise RefreshError("edited config.toml failed to re-parse — rolled back") from exc
    if parsed != expected:
        raise RefreshError("edited config.toml differs beyond the launch lines — rolled back")


# --- file I/O -----------------------------------------------------------------


def read_config(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(before.st_mode):
        raise RefreshError("config.toml is not a regular file (symlink?) — left untouched")
    if before.st_size > MAX_CONFIG_BYTES:
        raise RefreshError("config.toml is too large — left untouched")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RefreshError("config.toml is unreadable — left untouched") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RefreshError("config.toml changed while reading — left untouched")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), opened


def _signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def write_atomic(path: Path, payload: bytes, like: os.stat_result,
                 expected_sig: tuple[int, int, int, int] | None) -> None:
    """Replace ``path`` with ``payload`` keeping mode (and owner when root)."""
    directory = path.parent
    tmp = directory / f".{path.name}.mcp-pin.{os.getpid()}.{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(payload)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short write")
                view = view[n:]
            if os.geteuid() == 0 and (like.st_uid, like.st_gid) != (0, 0):
                os.fchown(fd, like.st_uid, like.st_gid)
            os.fchmod(fd, stat.S_IMODE(like.st_mode))
            os.fsync(fd)
        finally:
            os.close(fd)
        if expected_sig is not None:
            current = path.lstat()
            if _signature(current) != expected_sig:
                raise RefreshError("config.toml changed during the edit — left untouched")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_backup(backup_dir: Path, payload: bytes, now: _dt.datetime | None = None) -> Path:
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or _dt.datetime.now()).strftime("%Y%m%d")
    target = backup_dir / f"{BACKUP_PREFIX}{stamp}"
    n = 1
    while target.exists() or target.is_symlink():
        n += 1
        target = backup_dir / f"{BACKUP_PREFIX}{stamp}.{n}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            n_written = os.write(fd, view)
            if n_written <= 0:
                raise OSError("short write")
            view = view[n_written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


# --- driver -------------------------------------------------------------------


def _load(config: Path) -> tuple[bytes, os.stat_result, str]:
    raw, st = read_config(config)
    try:
        return raw, st, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RefreshError("config.toml is not UTF-8 — left untouched") from exc


def _apply(config: Path, raw: bytes, st: os.stat_result, new_text: str,
           backup_dir: Path) -> Path:
    """Back up, write, re-read and verify; restore the original on mismatch."""
    expected = _parse(new_text)
    try:
        backup = write_backup(backup_dir, raw)
    except OSError as exc:
        raise RefreshError("backup could not be written — config.toml left untouched") from exc
    try:
        write_atomic(config, new_text.encode("utf-8"), st, _signature(st))
    except OSError as exc:
        raise RefreshError("config.toml write failed — left untouched") from exc
    try:
        landed, _landed_st = read_config(config)
        verify(landed.decode("utf-8"), expected)
    except (RefreshError, OSError, UnicodeDecodeError) as exc:
        try:
            write_atomic(config, raw, st, None)
        except (RefreshError, OSError):
            raise RefreshError(
                f"written config.toml failed verification and restore failed — restore from {backup}"
            ) from exc
        raise RefreshError(
            f"written config.toml failed verification — original restored (backup: {backup})"
        ) from exc
    return backup


def refresh(codex_home: Path, *, backup_dir: Path, npm_root: str | None = None,
            dry_run: bool = False, out=sys.stdout, err=sys.stderr) -> int:
    config = codex_home / CONFIG_NAME
    try:
        raw, st, text = _load(config)
        if npm_root:
            roots: RootsSource = [npm_root]
        else:
            roots = functools.lru_cache(maxsize=1)(lambda: npm_global_roots(runtime_home(config)))
        new_text, changes, notes = plan(text, roots)
        if dry_run:
            for name, _cli in changes:
                print(f"  - Codex {name}: would re-pin npx -> node <global cli>", file=out)
            if not changes:
                print("  - Codex: no web MCP launch would change", file=out)
            return 0
        for note in notes:
            print(f"  - Codex {note}", file=out)
        if not changes:
            return 0
        backup = _apply(config, raw, st, new_text, backup_dir)
    except FileNotFoundError:
        print(f"  - Codex: no {CONFIG_NAME} — skipped", file=out)
        return 0
    except RefreshError as exc:
        print(f"  ! Codex: {exc}", file=err)
        return 1
    for name, cli in changes:
        print(f"  - Codex {name}: re-pinned to node {cli} (was npx; env kept)", file=out)
    print(f"  - Codex: backup at {backup}", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-pin registered Codex web MCP launches (npx -y) to the global install (node <cli>)."
    )
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    parser.add_argument(
        "--backup-dir",
        default=os.path.join(os.environ.get("CCC_CLAUDE_DIR") or os.path.expanduser("~/.claude"), "state", "backups"),
    )
    parser.add_argument("--npm-root", default=None,
                        help="global node_modules root to resolve packages from (default: the runtime user's `npm root -g`)")
    parser.add_argument("--dry-run", action="store_true", help="report only the server names that would change")
    args = parser.parse_args(argv)
    return refresh(Path(args.codex_home), backup_dir=Path(args.backup_dir),
                   npm_root=args.npm_root, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
