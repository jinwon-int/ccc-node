#!/usr/bin/env python3
"""Adversarial fixtures for streamed metadata; no fleet access or launches."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fleet_watch_metadata as meta


class MetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'home'
        self.repo = self.home / 'ccc-node'
        self.uid = os.getuid()
        for name in ('scripts/ccc-doctor.sh', 'claude/settings.base.json', 'bridge/start.sh'):
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('fixture\n')
        self.ref = self.home / '.claude/self-update.repo'
        self.ref.parent.mkdir(mode=0o700)
        self.ref.write_text(str(self.repo) + '\n')
        self.ref.chmod(0o600)

    def test_installed_reference_is_distinct_from_runtime(self):
        self.assertEqual(meta.installed_root(self.home, self.uid), self.repo)

    def test_unsafe_references_never_select_fallback(self):
        for value in ('relative', '/missing', str(self.repo) + '\nDOCTOR=0',
                      str(self.repo) + "'", str(self.repo / '../ccc-node')):
            with self.subTest(value=value):
                self.ref.write_text(value)
                with self.assertRaises((ValueError, OSError)):
                    meta.installed_root(self.home, self.uid)

    def test_reference_permissions_and_owner(self):
        for mode in (0o644, 0o660, 0o666):
            self.ref.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                meta.installed_root(self.home, self.uid)
        self.ref.chmod(0o600)
        with self.assertRaises(ValueError):
            meta.installed_root(self.home, self.uid + 1)

    def test_reference_symlink(self):
        other = self.ref.with_name('original')
        self.ref.rename(other)
        self.ref.symlink_to(other)
        with self.assertRaises(OSError):
            meta.installed_root(self.home, self.uid)

    def test_install_root_symlink_or_incomplete(self):
        alias = self.home / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.ref.write_text(str(alias))
        with self.assertRaises(ValueError):
            meta.installed_root(self.home, self.uid)
        self.ref.write_text(str(self.repo))
        (self.repo / 'scripts/ccc-doctor.sh').unlink()
        with self.assertRaises(OSError):
            meta.installed_root(self.home, self.uid)

    def test_fifo_record_does_not_block(self):
        self.ref.unlink()
        os.mkfifo(self.ref, 0o600)
        with self.assertRaises(ValueError):
            meta.installed_root(self.home, self.uid)

    def test_oversize_record(self):
        self.ref.write_text('x' * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            meta.installed_root(self.home, self.uid)

    def prepared(self):
        root = self.home / '.ccc-node/checkouts/staging'
        (root / 'bridge').mkdir(parents=True)
        (root / 'bridge/prepared_runtime.py').write_text('# validator fixture\n')
        self.git(root, 'init', '-q', '--initial-branch=main')
        self.git(root, 'add', '.')
        self.git(root, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid', 'commit', '-qm', 'fixture')
        head = self.git(root, 'rev-parse', 'HEAD')
        self.git(root, 'update-ref', 'refs/remotes/origin/main', head)
        self.root = root.with_name(head[:8])
        root.rename(self.root)
        self.job = self.home / '.ccc-node/preparations/fixture-api24'
        (self.job / 'runtime').mkdir(parents=True)
        self.job.chmod(0o700)
        seal = {'sha256': 'a' * 64, 'files': 1, 'bytes': 20}
        self.receipt = {'schema': 'ccc.termux-preparation.v1', 'status': 'ready',
                        'work_dir': str(self.job), 'source_seal': seal}
        self.write_receipt()
        self.report = {'schema': 'ccc.prepared-runtime.v1', 'status': 'ready',
                       'source_dir': str(self.root / 'bridge'),
                       'runtime_dir': str(self.job / 'runtime'),
                       'source_seal': seal, 'source_git': {'head': head, 'tracked_changes': False},
                       'checks': [{'id': c, 'status': 'pass'} for c in
                                  ['native_import', 'sdk_import', 'aes_gcm', 'pip_check']]}
        self.real_run = meta.owned_run

    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args],
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def write_receipt(self):
        p = self.job / 'receipt.json'
        p.write_text(json.dumps(self.receipt))
        p.chmod(0o600)

    def proof(self):
        def command(uid, argv, **kwargs):
            if argv[0] == 'git':
                return self.real_run(uid, argv, **kwargs)
            self.assertNotIn('--record-dir', argv)
            self.assertEqual(argv[:3], [str(self.job / 'runtime/bin/python'), '-I', '-B'])
            return json.dumps(self.report)
        with patch.object(meta, 'owned_run', side_effect=command):
            return meta.checkout_proof(self.root, self.job, self.home, self.uid)

    def test_clean_main_contained_checkout_and_exact_preparation(self):
        self.prepared()
        self.assertTrue(self.proof())

    def test_tracked_source_edit_rejected_before_validator(self):
        self.prepared()
        (self.root / 'bridge/prepared_runtime.py').write_text('# tampered\n')
        with self.assertRaises(ValueError):
            self.proof()

    def test_main_provenance_missing(self):
        self.prepared()
        self.git(self.root, 'update-ref', '-d', 'refs/remotes/origin/main')
        with self.assertRaises(subprocess.SubprocessError):
            self.proof()

    def test_other_commit_and_non_hex_checkout_rejected(self):
        self.prepared()
        for name in ('pr-123', 'abcdef0'):
            new = self.root.with_name(name)
            self.root.rename(new)
            self.root = new
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.proof()

    def test_unsafe_or_relocated_preparation(self):
        self.prepared()
        for key, value in [('schema', 'other'), ('status', 'failed'), ('work_dir', '/other'), ('source_seal', None)]:
            old = self.receipt[key]
            self.receipt[key] = value
            self.write_receipt()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.proof()
            self.receipt[key] = old
        self.write_receipt()
        self.job.chmod(0o777)
        with self.assertRaises(ValueError):
            self.proof()

    def test_validator_report_must_bind_all_identities(self):
        self.prepared()
        for key, value in [('status', 'unready'), ('schema', 'other'), ('source_dir', '/other'),
                           ('runtime_dir', '/other'), ('source_seal', {}), ('source_git', {}), ('checks', [])]:
            old = self.report[key]
            self.report[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.proof()
            self.report[key] = old

    def test_job_and_source_symlinks_rejected(self):
        self.prepared()
        alias = self.job.with_name('alias')
        alias.symlink_to(self.job, target_is_directory=True)
        self.job = alias
        with self.assertRaises(ValueError):
            self.proof()


if __name__ == '__main__':
    unittest.main()
