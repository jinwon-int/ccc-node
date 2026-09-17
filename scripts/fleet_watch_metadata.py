#!/usr/bin/env python3
"""Read-only fleet metadata; streamed by the watcher, never installed on peers.

The self-update.repo file is the operator-owned installation reference written
by setup.sh, not a guessed healthy checkout. Runtime provenance is independent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys


def physical(path: Path) -> Path:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("nonphysical_path")
    return path


def private_text(path: Path, uid: int) -> str:
    physical(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
            raise ValueError('unsafe_record')
        value = stream.read(1024 * 1024 + 1)
    if len(value) > 1024 * 1024:
        raise ValueError('oversize_record')
    return value.decode('utf-8')


def protected_path(path: Path, uid: int) -> None:
    """Reject foreign replacement/write access, including ancestor replacement.

    Root and the runtime owner are trusted. Android's fixed OS-owned app
    ancestors are a platform boundary, not arbitrary UID-1000 trust on Linux.
    Sticky shared temporary ancestors are safe only with protected children.
    """
    physical(path)
    android = hasattr(sys, 'getandroidapilevel')
    for item in (path, *path.parents):
        info = item.stat()
        is_dir = stat.S_ISDIR(info.st_mode)
        platform_parent = android and str(item) in ('/data', '/data/data')
        owners = {0, uid, 1000} if platform_parent else {0, uid}
        if info.st_uid not in owners:
            raise ValueError('foreign_path_owner')
        writable = info.st_mode & 0o022
        # Android assigns these app ancestors matching private app/system GIDs.
        app_parent = android and str(item) == '/data/data/com.termux/files'
        private_android_group = ((platform_parent or app_parent)
                                 and info.st_gid == info.st_uid
                                 and not info.st_mode & 0o002)
        sticky_ancestor = (item != path and is_dir
                           and info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
        if writable and not (private_android_group or sticky_ancestor):
            raise ValueError('foreign_writable_path')
        if not (is_dir or stat.S_ISREG(info.st_mode)):
            raise ValueError('nonregular_path')


def installed_root(project: Path, uid: int) -> Path:
    protected_path(project / '.claude', uid)
    value = private_text(project / '.claude/self-update.repo', uid).strip()
    # Line-oriented shell output must never become executable syntax or extra
    # probe keys. Supported operator paths have no whitespace/metacharacters.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
        raise ValueError('invalid_install_reference')
    root = physical(Path(value))
    for name in ('scripts/ccc-doctor.sh', 'claude/settings.base.json', 'bridge/start.sh'):
        path = physical(root / name)
        protected_path(path, uid)
        if not path.is_file():
            raise ValueError('incomplete_install_reference')
    # The wrapper imports Python modules/helpers from scripts. Protect those
    # too, including cached/import-shadow files, before invoking the wrapper.
    for path in (root / 'scripts').rglob('*'):
        protected_path(path, uid)
    checker = root / 'bridge/runtime_config_check.py'
    if checker.exists():
        protected_path(checker, uid)
    return root


def owned_run(uid: int, argv: list[str], *, timeout: int = 5) -> str:
    prefix: list[str] = []
    if uid != os.getuid():
        name = pwd.getpwuid(uid).pw_name
        prefix = (['runuser', '-u', name, '--'] if os.getuid() == 0
                  else ['sudo', '-n', '-H', '-u', name, '--'])
    result = subprocess.run(prefix + argv, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    return result.stdout.strip()


def checkout_proof(root: Path, job: Path, project: Path, uid: int) -> bool:
    """Accept the separate checkout layout only with main + live preparation proof.

    The shell first binds the supervisor UID, PPID, project, and interpreter.
    Reuse the launcher's read-only validator for receipt/source seal, editable
    source, dependency fingerprint and native imports; no record-dir is passed.
    """
    root, job, project = physical(root), physical(job), physical(project)
    if root.parent != project / '.ccc-node/checkouts':
        raise ValueError('unexpected_checkout_root')
    if not re.fullmatch(r'[0-9a-f]{7,40}', root.name):
        raise ValueError('invalid_checkout_name')
    if job.parent != project / '.ccc-node/preparations':
        raise ValueError('unexpected_preparation_root')
    head = owned_run(uid, ['git', '-C', str(root), 'rev-parse', 'HEAD'])
    if not re.fullmatch(r'[0-9a-f]{40}', head) or not head.startswith(root.name):
        raise ValueError('checkout_head_mismatch')
    if owned_run(uid, ['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no']):
        raise ValueError('dirty_checkout')
    owned_run(uid, ['git', '-C', str(root), 'merge-base', '--is-ancestor', head, 'refs/remotes/origin/main'])
    receipt = json.loads(private_text(job / 'receipt.json', uid))
    if (receipt.get('schema') != 'ccc.termux-preparation.v1'
            or receipt.get('status') != 'ready' or receipt.get('work_dir') != str(job)):
        raise ValueError('invalid_preparation')
    seal = receipt.get('source_seal')
    if (not isinstance(seal, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(seal.get('sha256', '')))
            or type(seal.get('files')) is not int or seal['files'] <= 0
            or type(seal.get('bytes')) is not int or seal['bytes'] <= 0):
        raise ValueError('invalid_source_seal')
    for directory, mask in ((job, 0o077), (job.parent, 0o022), (job / 'runtime', 0o022)):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & mask:
            raise ValueError('unsafe_preparation_directory')
    python = job / 'runtime/bin/python'
    report = json.loads(owned_run(uid, [str(python), '-I', '-B',
        str(root / 'bridge/prepared_runtime.py'), '--bridge-dir', str(root / 'bridge'),
        '--prepared-dir', str(job)], timeout=20))
    if (report.get('schema') != 'ccc.prepared-runtime.v1' or report.get('status') != 'ready'
            or report.get('source_dir') != str(root / 'bridge')
            or report.get('runtime_dir') != str(job / 'runtime')
            or report.get('source_seal') != receipt.get('source_seal')
            or report.get('source_git') != {'head': head, 'tracked_changes': False}
            or {c.get('id') for c in report.get('checks', []) if c.get('status') == 'pass'}
               != {'native_import', 'sdk_import', 'aes_gcm', 'pip_check'}):
        raise ValueError('preparation_validation_failed')
    return True


def main() -> int:
    try:
        mode, source, prepared, home, owner = sys.argv[1:]
        root, job, project, uid = Path(source), Path(prepared), Path(home), int(owner)
        if mode == 'installed':
            print(installed_root(project, uid))
        elif mode == 'checkout':
            checkout_proof(root, job, project, uid)
            print('verified')
        else:
            return 2
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return 1  # body-free, explicit failed inspection; never select a fallback


if __name__ == '__main__':
    raise SystemExit(main())
