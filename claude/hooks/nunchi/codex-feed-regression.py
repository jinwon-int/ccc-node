#!/usr/bin/env python3
"""Hermetic collector behavior: native transcripts, source growth, failure receipts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reader = load('session-tail')
receipts = load('feed-receipt')


def message(text, role='user'):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
            'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sessions = self.root / 'sessions with spaces'
        self.sessions.mkdir()
        self.source = self.sessions / 'rollout-test.jsonl'
        self.source.write_text(json.dumps(message('Synthetic conversation ' * 20)) + '\n')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.hooks = self.root / 'hooks'
        self.hooks.mkdir()
        for name in ('codex-feed.sh', 'session-tail.py', 'feed-receipt.py', 'feed-common.sh'):
            shutil.copy2(HERE / name, self.hooks / name)
        (self.hooks / 'nunchi.py').write_text('''import os,sys,json,pathlib
if sys.argv[1] == 'ingest':
    data=json.load(sys.stdin)
    if os.environ.get('FAIL_STORE') == '1': sys.exit(4)
    with open(os.environ['STORED'], 'a') as f: f.write(json.dumps(data)+'\\n')
''')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        binary = self.bin / 'codex'
        binary.write_text('#!' + sys.executable + '''
import os,json,pathlib,sys
with open(os.environ['CALLS'], 'a') as f: f.write('call\\n')
if os.environ.get('FAIL_EXTRACT') == '1': sys.exit(3)
print(json.dumps({'honcho':[{'kind':'fact','text':'A synthetic fact','subject':'session'}]}))
''')
        binary.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.root), CCC_NUNCHI_MODE='on',
                        CCC_STATE_DIR=str(self.root / 'state'), NUNCHI_HOME=str(self.home),
                        CODEX_SESSIONS_DIR=str(self.sessions), CALLS=str(self.root / 'calls'),
                        STORED=str(self.root / 'stored'), PATH=str(self.bin) + ':' + os.environ['PATH'])

    def run_feed(self, **env):
        result = subprocess.run(['bash', str(self.hooks / 'codex-feed.sh')], env=dict(self.env, **env),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def stored(self):
        path = self.root / 'stored'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_native_format_tool_exclusion_and_legacy_fallback(self):
        self.source.write_text('\n'.join(json.dumps(x) for x in [message('real user'),
            message('real assistant', 'assistant'), message('hidden system', 'system'),
            {'type':'response_item','payload':{'type':'function_call_output','output':'tool secret'}},
            {'type':'event_msg','payload':{'type':'user_message','message':'duplicate legacy'}}]))
        text = reader.read('codex', self.source, self.sessions)
        self.assertEqual(text, 'USER: real user\nAGENT: real assistant')
        self.source.write_text(json.dumps({'type':'event_msg','payload':{'type':'user_message','message':'old format'}}))
        self.assertIn('old format', reader.read('codex', self.source, self.sessions))

    def test_growth_is_reprocessed_but_unchanged_file_is_not(self):
        self.run_feed()
        self.assertEqual(self.stored(), 1)
        self.run_feed()
        self.assertEqual(self.stored(), 1)
        with self.source.open('a') as f:
            f.write(json.dumps(message('Later durable decision ' * 20)) + '\n')
        self.run_feed()
        self.assertEqual(self.stored(), 2)
        self.assertEqual(stat.S_IMODE((self.home / 'codex-receipts.jsonl').stat().st_mode), 0o600)

    def test_malformed_records_do_not_discard_valid_conversation(self):
        bad = [None, [], {'type': 'event_msg', 'payload': 'bad-shape'},
               {'type': 'response_item', 'payload': ['bad']},
               {'type': 'response_item', 'payload': {'type': 'message', 'role': []}},
               {'type': 'event_msg', 'payload': {'type': [], 'message': 'bad'}},
               {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 42}},
               {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': [], 'text': 'bad'}]}}]
        good = 'Valid durable conversation ' * 20
        self.source.write_text('\n'.join(json.dumps(row) for row in [message(good), *bad, message('valid answer', 'assistant')]))
        self.assertEqual(reader.read('codex', self.source, self.sessions), 'USER: ' + good + '\nAGENT: valid answer')
        self.run_feed()
        self.assertEqual(self.stored(), 1)
        row = json.loads((self.home / 'codex-receipts.jsonl').read_text().splitlines()[-1])
        self.assertEqual(row['status'], 'stored')

    def test_legacy_seen_does_not_hide_recent_native_transcript(self):
        (self.home / 'codex-seen').write_text(str(self.source) + '\n')
        self.run_feed()
        self.assertEqual(self.stored(), 1)

    def test_extraction_failure_remains_retryable_with_backoff(self):
        self.run_feed(FAIL_EXTRACT='1')
        path = self.home / 'codex-receipts.jsonl'
        row = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(row['status'], 'failed')
        self.run_feed()
        self.assertEqual(self.stored(), 0)
        row['at'] = 0
        path.write_text(json.dumps(row) + '\n')
        self.run_feed()
        self.assertEqual(self.stored(), 1)

    def test_failed_storage_never_gets_a_success_receipt(self):
        self.run_feed(FAIL_STORE='1')
        row = json.loads((self.home / 'codex-receipts.jsonl').read_text().splitlines()[-1])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(self.stored(), 0)

    def test_changed_source_cannot_acknowledge_an_older_snapshot(self):
        _, token, _ = receipts.fingerprint(self.source)
        with self.source.open('a') as f:
            f.write('\n')
        receipt = self.home / 'receipt'
        self.assertEqual(receipts.main(['stored', str(receipt), str(self.source), token]), 4)
        self.assertFalse(receipt.exists())

    def test_symlink_source_and_receipt_are_rejected(self):
        link = self.sessions / 'linked.jsonl'
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            reader.read('codex', link, self.sessions)
        target = self.root / 'untouched'
        target.write_text('sentinel')
        receipt = self.home / 'receipt'
        receipt.symlink_to(target)
        _, token, _ = receipts.fingerprint(self.source)
        with self.assertRaises((ValueError, OSError)):
            receipts.main(['stored', str(receipt), str(self.source), token])
        self.assertEqual(target.read_text(), 'sentinel')

    def test_feed_rejects_symlink_lock_without_touching_target(self):
        target = self.root / 'lock-target'
        target.write_text('sentinel')
        (self.home / '.codex-feed.lock').symlink_to(target)
        result = subprocess.run(['bash', str(self.hooks / 'codex-feed.sh')], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), 'sentinel')
        self.assertEqual(self.stored(), 0)

    def test_busy_feed_lock_defers_without_extraction(self):
        import fcntl
        with (self.home / '.codex-feed.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_feed()
            self.assertEqual(self.stored(), 0)
        self.run_feed()
        self.assertEqual(self.stored(), 1)

    def test_large_source_reads_only_bounded_tail(self):
        with self.source.open('w') as f:
            f.write('x' * (reader.CAP + 100) + '\n')
            f.write(json.dumps(message('latest text')) + '\n')
        self.assertEqual(reader.read('codex', self.source, self.sessions), 'USER: latest text')


if __name__ == '__main__':
    unittest.main()
