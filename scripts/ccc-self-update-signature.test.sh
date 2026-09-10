#!/usr/bin/env bash
# Commit-signature verification for the self-update tip (#1591).
#
# Local git fixtures only; no network, no real self-update run, no service
# lifecycle. Signing is exercised with a throwaway GPG key generated inside the
# fixture, so the suite never touches the node keyring or the vendored key.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 - "$HERE/ccc-self-update.sh" <<'PY'
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(sys.argv.pop())


def _q(value):
    return shlex.quote(str(value))


def extract_function(name):
    """Pull one shell function body out of the script under test.

    The suite must exercise the real implementation, not a restatement of it:
    if the function is renamed or deleted, extraction fails and the suite goes
    red instead of silently testing nothing.
    """
    text = SCRIPT.read_text()
    match = re.search(rf'^{name}\(\) \{{.*?^\}}', text, re.S | re.M)
    if match is None:
        raise AssertionError(f'{name}() not found in {SCRIPT}')
    return match.group(0)


GPG = shutil.which('gpg')
GIT = shutil.which('git')
BASH = shutil.which('bash') or '/bin/bash'


@unittest.skipUnless(GPG and GIT, 'gpg and git are required')
class VerifyCommitSignatureTest(unittest.TestCase):
    """Each case builds a real signed/unsigned commit and asks the real function."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='ccc-sig-test-')
        root = Path(cls._tmp.name)
        cls.gnupghome = root / 'gnupg'
        cls.gnupghome.mkdir(mode=0o700)
        cls.env = dict(os.environ, GNUPGHOME=str(cls.gnupghome))

        # A throwaway signing key standing in for GitHub's web-flow key.
        subprocess.run(
            [GPG, '--batch', '--quiet', '--passphrase', '', '--yes',
             '--quick-generate-key', 'ccc-test-signer@example.invalid',
             'default', 'default', 'never'],
            env=cls.env, check=True, capture_output=True)
        listing = subprocess.run(
            [GPG, '--batch', '--with-colons', '--list-keys'],
            env=cls.env, check=True, capture_output=True, text=True).stdout
        cls.fpr = next(line.split(':')[9] for line in listing.splitlines()
                       if line.startswith('fpr:'))
        cls.keyring = root / 'trusted.gpg'
        cls.keyring.write_bytes(subprocess.run(
            [GPG, '--batch', '--export', cls.fpr],
            env=cls.env, check=True, capture_output=True).stdout)

        # A repo whose two commits differ only in whether they are signed.
        cls.repo = root / 'repo'
        cls.repo.mkdir()
        cls._git('init', '-q', '-b', 'main')
        cls._git('config', 'user.email', 'ccc-test-signer@example.invalid')
        cls._git('config', 'user.name', 'ccc test signer')
        cls._git('config', 'user.signingkey', cls.fpr)
        cls._git('config', 'gpg.program', GPG)
        (cls.repo / 'f').write_text('base\n')
        cls._git('add', 'f')
        cls._git('commit', '-qm', 'signed base', '-S')
        cls.signed = cls._git('rev-parse', 'HEAD').strip()
        (cls.repo / 'f').write_text('tampered\n')
        cls._git('commit', '-aqm', 'unsigned attacker commit', '--no-gpg-sign')
        cls.unsigned = cls._git('rev-parse', 'HEAD').strip()

    @classmethod
    def _git(cls, *args):
        return subprocess.run([GIT, '-C', str(cls.repo), *args], env=cls.env,
                              check=True, capture_output=True, text=True).stdout

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def verify(self, rev, *, trusted=None, keyring=None, path=None):
        """Invoke the real verify_commit_signature() and return (result, rc)."""
        harness = '\n'.join([
            'set -u',
            f'REPO={_q(str(self.repo))}',
            f'SELF_UPDATE_DIR={_q(str(SCRIPT.parent))}',
            f'SIGNATURE_KEYRING={_q(keyring if keyring is not None else str(self.keyring))}',
            f'TRUSTED_SIGNING_FPRS={_q(self.fpr if trusted is None else trusted)}',
            extract_function('verify_commit_signature'),
            f'verify_commit_signature {_q(rev)}',
        ])
        env = dict(self.env)
        if path is not None:
            # Emptying PATH is how the "node has no gpg" case is simulated, so
            # bash itself must be invoked by absolute path.
            env['PATH'] = path
        proc = subprocess.run([BASH, '-c', harness], env=env,
                              capture_output=True, text=True)
        return proc.stdout.strip(), proc.returncode

    def test_signed_commit_from_pinned_key_is_ok(self):
        self.assertEqual(self.verify(self.signed), ('ok', 0))

    def test_unsigned_commit_is_rejected(self):
        # The core attack: a commit pushed directly to the branch, bypassing
        # the GitHub merge path that produces the signature.
        result, rc = self.verify(self.unsigned)
        self.assertNotEqual(result, 'ok')
        self.assertNotEqual(rc, 0)

    def test_good_signature_from_unpinned_key_is_rejected(self):
        # Guards the fingerprint pin itself: a GOODSIG from any key the keyring
        # happens to hold must NOT satisfy verification.
        result, rc = self.verify(self.signed, trusted='0' * 40)
        self.assertEqual(result, 'unverified')
        self.assertNotEqual(rc, 0)

    def test_missing_keyring_fails_closed(self):
        result, rc = self.verify(self.signed, keyring='/nonexistent/key.gpg')
        self.assertEqual(result, 'no-keyring')
        self.assertNotEqual(rc, 0)

    def test_absent_gpg_fails_closed(self):
        # A node without gpg must not be treated as verified.
        result, rc = self.verify(self.signed, path='/nonexistent-bin')
        self.assertEqual(result, 'no-gpg')
        self.assertNotEqual(rc, 0)


class VendoredKeyringTest(unittest.TestCase):
    """The shipped default keyring must exist and contain the pinned keys."""

    def test_default_keyring_is_present_and_pinned(self):
        keyring = SCRIPT.parent / 'trusted-keys' / 'github-web-flow.gpg'
        self.assertTrue(keyring.is_file(), f'missing vendored keyring: {keyring}')
        script = SCRIPT.read_text()
        self.assertIn('968479A1AFF927E37D1A566BB5690EEEBB952194', script)
        if GPG:
            listing = subprocess.run(
                [GPG, '--batch', '--show-keys', '--with-colons', str(keyring)],
                check=True, capture_output=True, text=True).stdout
            fprs = {line.split(':')[9] for line in listing.splitlines()
                    if line.startswith('fpr:')}
            self.assertIn('968479A1AFF927E37D1A566BB5690EEEBB952194', fprs)

    def test_setup_installs_the_keyring_beside_the_deployed_hook(self):
        """The keyring must follow the hook to its DEPLOYED location (#1599).

        This suite previously only proved the key exists in the repo. But the
        hook runs from $CLAUDE_DIR/hooks and resolves the keyring relative to
        itself, so the repo copy is irrelevant at runtime. setup.sh shipped the
        script without the key, and every deployed node reported `no-keyring`
        forever — verification could never go green, and `enforce` would have
        stopped the entire fleet. Observed live on yukson before this fix.
        """
        setup = (SCRIPT.parent.parent / 'setup.sh').read_text()
        hook_install = setup.index(
            'atomic_install "$SRC/scripts/ccc-self-update.sh"')
        key_install = setup.index(
            'atomic_install "$SRC/scripts/trusted-keys/github-web-flow.gpg"')
        # The destination must be under the hooks dir, next to the script.
        self.assertIn(
            '"$CLAUDE_DIR/hooks/trusted-keys/github-web-flow.gpg"',
            setup[key_install:key_install + 400])
        # atomic_install does not create directories.
        self.assertIn('mkdir -p "$CLAUDE_DIR/hooks/trusted-keys"', setup)
        self.assertLess(hook_install, key_install)


@unittest.skipUnless(GPG, 'gpg is required')
class DeployedKeyringResolutionTest(unittest.TestCase):
    """Run the real setup.sh, then verify from the DEPLOYED layout (#1599).

    The bug this guards was invisible to source-text and repo-path assertions:
    everything looked right in the checkout while the deployed hook resolved a
    keyring path that setup.sh never populated. So install for real into a
    throwaway CLAUDE_DIR and ask the deployed copy to verify.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='ccc-deploy-test-')
        root = Path(cls._tmp.name)
        cls.claude_dir = root / 'claude'
        repo = SCRIPT.parent.parent
        env = dict(os.environ,
                   HOME=str(root / 'home'),
                   CCC_CLAUDE_DIR=str(cls.claude_dir),
                   CCC_HERMES_DIR=str(root / 'hermes'),
                   # setup.sh refuses to bake an ephemeral HOME into the live
                   # systemd tree (#885); route unit writes into the fixture.
                   CCC_SYSTEMD_DIR=str(root / 'systemd'),
                   CCC_SETUP_ALLOW_OWNER_MISMATCH='1')
        (root / 'home').mkdir()
        cls.proc = subprocess.run([BASH, str(repo / 'setup.sh')], env=env,
                                  cwd=str(repo), capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_setup_succeeded(self):
        self.assertEqual(self.proc.returncode, 0,
                         f'setup.sh failed:\n{self.proc.stdout[-2000:]}\n'
                         f'{self.proc.stderr[-2000:]}')

    def test_keyring_lands_next_to_the_deployed_hook(self):
        hook = self.claude_dir / 'hooks' / 'ccc-self-update.sh'
        self.assertTrue(hook.is_file(), 'hook not deployed')
        keyring = hook.parent / 'trusted-keys' / 'github-web-flow.gpg'
        self.assertTrue(
            keyring.is_file(),
            'deployed hook resolves its keyring here and setup.sh must put it '
            f'there: {keyring}')

    def test_deployed_hook_resolves_its_default_keyring(self):
        # The end-to-end assertion: no explicit keyring, default resolution
        # only, against the repo's own signed HEAD.
        repo = SCRIPT.parent.parent
        hook = self.claude_dir / 'hooks' / 'ccc-self-update.sh'
        harness = '\n'.join([
            'set -u',
            f'REPO={_q(str(repo))}',
            f'SELF_UPDATE_DIR={_q(str(hook.parent))}',
            'SIGNATURE_KEYRING=""',
            'TRUSTED_SIGNING_FPRS="968479A1AFF927E37D1A566BB5690EEEBB952194\n'
            '5DE3E0509C47EA3CF04A42D34AEE18F83AFDEB23"',
            extract_function('verify_commit_signature'),
            'verify_commit_signature HEAD',
        ])
        proc = subprocess.run([BASH, '-c', harness], capture_output=True, text=True)
        self.assertNotEqual(
            proc.stdout.strip(), 'no-keyring',
            'deployed layout cannot find its keyring — the #1599 regression')


class SignatureGateWiringTest(unittest.TestCase):
    """Pin the call-site contract, not just the helper."""

    def setUp(self):
        self.text = SCRIPT.read_text()

    def test_verification_runs_before_the_merge(self):
        # Verifying after the ff-merge would already have moved the checkout
        # onto unverified code, which setup.sh then executes.
        gate = self.text.index('verify_commit_signature "origin/$BRANCH"')
        merge = self.text.index('merge --ff-only "origin/$BRANCH"')
        self.assertLess(gate, merge)

    def test_enforce_mode_aborts_with_a_distinct_exit_code(self):
        self.assertIn('exit 13', self.text)
        self.assertIn('unverified-signature', self.text)

    def test_default_mode_is_documented_rollout_default(self):
        self.assertIn('CCC_SELF_UPDATE_SIGNATURE_MODE:-warn', self.text)


def extract_gate():
    """Pull the signature gate block out of the script under test."""
    text = SCRIPT.read_text()
    start = text.index('if [ "$SIGNATURE_MODE" != "off" ]; then')
    end = text.index('if ! git -C "$REPO" merge --ff-only', start)
    return text[start:end]


class SignatureGateBehaviourTest(unittest.TestCase):
    """Run the real gate block with a stubbed verifier and a stubbed git.

    Text assertions cannot tell whether an up-to-date tick actually calls the
    verifier — the bug in #1597 was exactly that it did not, while logging a
    line identical to a real verification. So execute the block and observe.
    """

    def run_gate(self, *, incoming, old, mode, verdict):
        harness = '\n'.join([
            'set -u',
            f'SIGNATURE_MODE={_q(mode)}',
            f'OLD_SHA={_q(old)}',
            'REPO=/nonexistent',
            'BRANCH=main',
            'LOG_LINES=""',
            'log() { printf "LOG:%s\\n" "$*"; }',
            'say() { :; }',
            'notify_stalled() { printf "STALLED:%s\\n" "$1"; }',
            # Stub git rev-parse to hand back the incoming tip.
            f'git() {{ printf "%s" {_q(incoming)}; }}',
            # Stub the verifier so we can see whether it ran at all.
            f'verify_commit_signature() {{ printf "CALLED\\n" >&2; printf "%s" {_q(verdict)}; '
            f'[ {_q(verdict)} = ok ]; }}',
            extract_gate(),
        ])
        proc = subprocess.run([BASH, '-c', harness], capture_output=True, text=True)
        return proc

    def test_up_to_date_tick_still_verifies(self):
        # The core regression: no short-circuit on an unchanged tip.
        p = self.run_gate(incoming='abc', old='abc', mode='warn', verdict='ok')
        self.assertIn('CALLED', p.stderr)
        self.assertIn('changed=no', p.stdout)
        self.assertIn('signature ok', p.stdout)

    def test_changed_tip_is_logged_as_changed(self):
        p = self.run_gate(incoming='def', old='abc', mode='warn', verdict='ok')
        self.assertIn('CALLED', p.stderr)
        self.assertIn('changed=yes', p.stdout)

    def test_enforce_aborts_only_when_the_tip_actually_changed(self):
        p = self.run_gate(incoming='def', old='abc', mode='enforce', verdict='no-gpg')
        self.assertIn('STALLED:unverified-signature', p.stdout)
        self.assertEqual(p.returncode, 13)

    def test_enforce_does_not_abort_an_up_to_date_tick(self):
        # Refusing here would protect nothing (that code already runs) while
        # cutting the node off from the update that would fix it.
        p = self.run_gate(incoming='abc', old='abc', mode='enforce', verdict='no-gpg')
        self.assertNotIn('STALLED', p.stdout)
        self.assertNotEqual(p.returncode, 13)
        self.assertIn('changed=no', p.stdout)
        self.assertIn('no-gpg', p.stdout)

    def test_off_mode_does_not_verify_at_all(self):
        p = self.run_gate(incoming='def', old='abc', mode='off', verdict='ok')
        self.assertNotIn('CALLED', p.stderr)
        self.assertEqual(p.stdout, '')


result = unittest.main(exit=False).result
failed = len(result.failures) + len(result.errors)
print(f"PASS={max(0, result.testsRun - failed)} FAIL={failed}")
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
