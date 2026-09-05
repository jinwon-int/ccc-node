"""Exercise native repair with process fixtures, never a live package install."""

import io
import os
import stat
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from telegram_bot.dependency_bootstrap import (
    DependencyPaths, InstallMode, dependency_fingerprint, sync_dependencies,
)
from telegram_bot.termux_native import ensure_termux_cryptography


class TermuxNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.venv = self.root / "venv with spaces"
        self.bin = self.venv / "bin"
        self.bin.mkdir(parents=True)
        self.extension = self.venv / "lib" / "_rust.abi3.so"
        self.extension.parent.mkdir()
        self.extension.write_bytes(b"broken")
        self.python = self.bin / "python"
        self._script(self.python, '''
import json, os, pathlib, sys
p = pathlib.Path(os.environ['EXTENSION'])
code = sys.argv[2]
if 'sysconfig' in code:
    print(json.dumps([str(p), 'libpython3.14.so']))
elif 'ctypes.CDLL' in code:
    sys.exit(0 if pathlib.Path(sys.argv[3]).read_bytes() == b'linked' else 1)
elif 'cryptography.exceptions' in code:
    if p.read_bytes() != b'linked' or os.environ.get('IMPORT_AFTER_PATCH_FAIL'):
        print('ImportError: dlopen failed: cannot locate symbol "PyLong_Type"', file=sys.stderr)
        sys.exit(1)
elif 'importlib' in code:
    sys.exit(0)
else:
    raise AssertionError(code)
''')
        self.patchelf = self.bin / "patchelf"
        self._script(self.patchelf, '''
import os, pathlib, sys
if sys.argv[1] == '--print-needed':
    print('libc.so')
elif sys.argv[1] == '--add-needed':
    assert sys.argv[2] == 'libpython3.14.so'
    with open(os.environ['PATCH_CALLS'], 'a') as log: log.write('patch\\n')
    if os.environ.get('PATCH_FAIL'): sys.exit(1)
    pathlib.Path(sys.argv[3]).write_bytes(b'linked')
''')
        self.env = {
            'PATH': str(self.bin), 'TERMUX_VERSION': '0.118',
            'EXTENSION': str(self.extension), 'PATCH_CALLS': str(self.root / 'calls'),
        }

    def _script(self, path, source):
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o700)

    def run_repair(self):
        out = io.StringIO()
        rc = ensure_termux_cryptography(self.python, self.venv, self.env, out)
        return rc, out.getvalue()

    def test_repair_preserves_original_and_loads_native_extension(self):
        self.assertEqual(self.run_repair()[0], 0)
        self.assertEqual(self.extension.read_bytes(), b'linked')
        backup, = self.extension.parent.glob('.ccc-native-recovery-*/original.so')
        self.assertEqual(backup.read_bytes(), b'broken')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.extension.stat().st_mode), 0o600)
        self.assertEqual(self.run_repair()[0], 0)
        self.assertEqual((self.root / 'calls').read_text(), 'patch\n')

    def test_concurrent_repair_rechecks_under_lock(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_repair()[0], range(2)))
        self.assertEqual(results, [0, 0])
        self.assertEqual((self.root / 'calls').read_text(), 'patch\n')

    def test_pip_reconciliation_waits_for_other_projects_native_repair(self):
        from telegram_bot import termux_native
        paths = DependencyPaths.from_roots(self.root, self.venv, self.root / 'project.env')
        paths.lock.write_text('fixture lock')
        paths.hash_cache.write_text(dependency_fingerprint(paths, InstallMode.LOCKED))
        marker = self.root / 'pip-entered'
        self._script(paths.pip, f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
        entered = threading.Event()
        release = threading.Event()
        original_repair = termux_native._repair

        def paused_repair(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test failed to release repair')
            return original_repair(*args)

        def sync(force):
            return sync_dependencies(paths, InstallMode.LOCKED, force_install=force,
                                     environ=self.env, stdout=io.StringIO())

        with patch.object(termux_native, '_repair', side_effect=paused_repair):
            with ThreadPoolExecutor(max_workers=2) as pool:
                repair = pool.submit(sync, False)
                try:
                    self.assertTrue(entered.wait(5))
                    installer = pool.submit(sync, True)
                    # The second caller must not reach its actual pip process
                    # until the first caller has completed the native repair.
                    self.assertFalse(threading.Event().wait(0.2) or marker.exists())
                finally:
                    release.set()
                self.assertEqual(repair.result(timeout=10), 0)
                self.assertEqual(installer.result(timeout=10), 0)
        self.assertTrue(marker.exists())

    def test_healthy_native_import_needs_no_patchelf(self):
        self.extension.write_bytes(b'linked')
        self.env['PATH'] = str(self.root / 'missing-bin')
        self.assertEqual(self.run_repair(), (0, ''))
        self.assertFalse((self.root / 'calls').exists())

    def test_missing_patchelf_fails_with_actionable_instruction(self):
        self.env['PATH'] = str(self.root / 'missing-bin')
        rc, out = self.run_repair()
        self.assertEqual(rc, 1)
        self.assertIn('pkg install patchelf', out)
        self.assertEqual(self.extension.read_bytes(), b'broken')

    def test_non_termux_never_probes_or_mutates(self):
        self.env.pop('TERMUX_VERSION')
        self.python.unlink()
        self.assertEqual(self.run_repair(), (0, ''))
        self.assertFalse((self.root / 'calls').exists())

    def test_failed_patch_leaves_original_and_backup(self):
        self.env['PATCH_FAIL'] = '1'
        self.assertEqual(self.run_repair()[0], 1)
        self.assertEqual(self.extension.read_bytes(), b'broken')
        self.assertEqual(len(list(self.extension.parent.glob('.ccc-native-recovery-*/original.so'))), 1)

    def test_failed_import_restores_original_and_preserves_failed_candidate(self):
        self.env['IMPORT_AFTER_PATCH_FAIL'] = '1'
        self.assertEqual(self.run_repair()[0], 1)
        self.assertEqual(self.extension.read_bytes(), b'broken')
        failed, = self.extension.parent.glob('.ccc-native-recovery-*/failed.so')
        self.assertEqual(failed.read_bytes(), b'linked')

    def test_symlink_extension_rejected(self):
        original = self.root / 'external.so'
        self.extension.rename(original)
        self.extension.symlink_to(original)
        self.assertEqual(self.run_repair()[0], 1)
        self.assertEqual(original.read_bytes(), b'broken')
        self.assertFalse((self.root / 'calls').exists())

    def test_symlink_parent_rejected(self):
        original = self.root / 'external-dir'
        self.extension.parent.rename(original)
        self.extension.parent.symlink_to(original, target_is_directory=True)
        self.assertEqual(self.run_repair()[0], 1)
        self.assertEqual((original / self.extension.name).read_bytes(), b'broken')

    def test_hardlink_extension_rejected(self):
        os.link(self.extension, self.root / 'external.so')
        self.assertEqual(self.run_repair()[0], 1)
        self.assertFalse((self.root / 'calls').exists())

    def test_symlink_lock_cannot_clobber_external_file(self):
        target = self.root / 'external-lock'
        target.write_text('unchanged')
        (self.venv / '.termux-native.lock').symlink_to(target)
        self.assertEqual(self.run_repair()[0], 1)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_extension_outside_venv_rejected(self):
        target = self.root / 'external.so'
        target.write_bytes(b'broken')
        self.env['EXTENSION'] = str(target)
        self.assertEqual(self.run_repair()[0], 1)
        self.assertEqual(target.read_bytes(), b'broken')

    def test_cache_hit_repairs_reinstalled_broken_wheel(self):
        paths = DependencyPaths.from_roots(self.root, self.venv, self.root / 'project.env')
        paths.hash_cache.write_text(dependency_fingerprint(paths, InstallMode.LOCKED))
        for _ in range(2):
            self.extension.write_bytes(b'broken')
            self.assertEqual(sync_dependencies(paths, InstallMode.LOCKED,
                                              environ=self.env, stdout=io.StringIO()), 0)
            self.assertEqual(self.extension.read_bytes(), b'linked')
        self.assertEqual((self.root / 'calls').read_text(), 'patch\npatch\n')

    def test_install_failure_does_not_publish_success_fingerprint(self):
        paths = DependencyPaths.from_roots(self.root, self.venv, self.root / 'project.env')
        paths.lock.write_text('fixture lock')
        self._script(paths.pip, 'raise SystemExit(0)\n')
        self.env['PATCH_FAIL'] = '1'
        self.assertEqual(sync_dependencies(paths, InstallMode.LOCKED,
                                          environ=self.env, stdout=io.StringIO()), 1)
        self.assertFalse(paths.hash_cache.exists())


class NativeSmokeRegressionTests(unittest.TestCase):
    def test_package_import_success_does_not_hide_native_import_failure(self):
        from telegram_bot.dependency_bootstrap import smoke_import_binary_extensions
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / 'cryptography' / 'hazmat' / 'bindings'
            package.mkdir(parents=True)
            for parent in [package, package.parent, package.parent.parent]:
                (parent / '__init__.py').write_text('')
            (package / '_rust.py').write_text('raise ImportError("broken native extension")')
            (root / 'claude_agent_sdk.py').write_text('')
            bin_dir = root / 'venv' / 'bin'
            bin_dir.mkdir(parents=True)
            (bin_dir / 'python').symlink_to(sys.executable)
            paths = DependencyPaths.from_roots(root, root / 'venv', root / 'env')
            out = io.StringIO()
            self.assertEqual(smoke_import_binary_extensions(
                paths, environ={'PYTHONPATH': str(root)}, stdout=out), 1)
            self.assertIn('broken native extension', out.getvalue())
