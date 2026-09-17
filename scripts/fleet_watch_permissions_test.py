#!/usr/bin/env python3
"""Actual Git fetch oracle for the watcher's read-only permission predicate."""
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile
import unittest


class GitAccessTest(unittest.TestCase):
    def test_fetch_head_and_immutable_objects(self):
        prefix = []
        uid, gid = os.getuid(), os.getgid()
        if uid == 0:
            try:
                account = pwd.getpwnam('nobody')
            except KeyError:
                self.skipTest('requires an unprivileged test account when running as root')
            if not shutil.which('runuser'):
                self.skipTest('requires runuser when running as root')
            uid, gid = account.pw_uid, account.pw_gid
            prefix = ['runuser', '-u', account.pw_name, '--']
        # Use the platform's shared temp parent, not a root-private harness
        # TMPDIR: the real unprivileged child must traverse the fixture parent.
        parent = '/tmp' if Path('/tmp').is_dir() else tempfile.gettempdir()
        with tempfile.TemporaryDirectory(prefix='fleet-permission-', dir=parent) as tmp:
            fixture = Path(tmp)
            fixture.chmod(0o755)
            repo, origin = fixture / 'repo', fixture / 'origin.git'
            def run(argv, user=False):
                return subprocess.run((prefix if user else []) + argv, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={**os.environ, 'LC_ALL': 'C'}, timeout=15)
            def checked(argv, user=False):
                result = run(argv, user)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            checked(['git', 'init', '-q', '--bare', '--initial-branch=main', str(origin)])
            checked(['git', 'init', '-q', '--initial-branch=main', str(repo)])
            checked(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                     'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture', '--allow-empty'])
            checked(['git', '-C', str(repo), 'remote', 'add', 'origin', str(origin)])
            checked(['git', '-C', str(repo), 'push', '-q', 'origin', 'main'])
            if prefix:
                for p in [repo, origin, *repo.rglob('*'), *origin.rglob('*')]:
                    os.chown(p, uid, gid)
            for p in (repo / '.git/objects').rglob('*'):
                if p.is_file():
                    if prefix:
                        os.chown(p, 0, 0)
                    p.chmod(0o444)
            code = Path(__file__).with_name('fleet-bridge-watch.sh').read_text()
            match = re.search(r'if dd_access=\$\(su - gongmyoung -c "(.*?)" 2>/dev/null\); then', code)
            self.assertIsNotNone(match)
            predicate = match[1].replace('$repo', str(repo))
            self.assertEqual(checked(['sh', '-c', predicate], True), '')
            checked(['git', '-C', str(repo), 'fetch', 'origin', 'main'], True)
            fetch_head = repo / '.git/FETCH_HEAD'
            if prefix:
                os.chown(fetch_head, 0, 0)
            fetch_head.chmod(0o444)
            self.assertEqual(checked(['git', '-C', str(repo), 'status', '--porcelain'], True), '')
            self.assertEqual(checked(['sh', '-c', predicate], True), '.git/FETCH_HEAD')
            result = run(['git', '-C', str(repo), 'fetch', 'origin', 'main'], True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Permission denied', result.stderr)
            if prefix:
                os.chown(fetch_head, uid, gid)
            fetch_head.chmod(0o644)
            self.assertEqual(checked(['sh', '-c', predicate], True), '')
            checked(['git', '-C', str(repo), 'fetch', 'origin', 'main'], True)


if __name__ == '__main__':
    unittest.main()
