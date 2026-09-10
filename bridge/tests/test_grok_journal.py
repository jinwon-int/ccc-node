"""Private append-only journal failure and process boundary regressions."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from telegram_bot.core.grok_journal import GrokBinding, GrokJournal, canonical
from telegram_bot.core.grok_protocol import AcceptedPrompt, Baseline, BoundReply, ProtocolError

AGENT = "00000000-0000-4000-8000-000000000001"


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.binding = GrokBinding("box@fixture.invalid", AGENT, "owner-dm", self.tmp.name)
        self.root = Path(self.tmp.name) / "journal"
        self.journal = GrokJournal(self.root, self.binding)
        self.journal.create()

    def operation(self, claim):
        op = claim.attempt("generated input", Baseline("before", ("prior",)))
        accepted = AcceptedPrompt(AGENT, op["nonce"], op["digest"], "echo")
        op = claim.accept(op, accepted)
        return claim.complete(op, BoundReply("new", ("reply",), ("generated output",)))

    def test_append_only_roundtrip_modes_and_binding(self):
        initial = (self.root / "0000.json").read_bytes()
        with self.journal.claim() as claim:
            self.operation(claim)
            state, count, _ = claim.load()
            self.assertEqual(state["stage"], "complete")
            self.assertEqual(count, 4)
        self.assertEqual((self.root / "0000.json").read_bytes(), initial)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for p in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        for binding in (replace(self.binding, conversation_id="other"),
                        replace(self.binding, destination="box@other.invalid")):
            with self.assertRaises(ProtocolError), GrokJournal(self.root, binding).claim() as claim:
                claim.load()

    def test_unsafe_files_and_unknown_state_retained(self):
        for kind in ("symlink", "hardlink", "fifo", "directory", "mode", "unknown", "oversize"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                journal = GrokJournal(Path(directory) / "j", self.binding)
                journal.create()
                p = journal.root / "0000.json"
                original = journal.root / "preserved-original"
                p.rename(original)
                if kind == "symlink":
                    p.symlink_to(original)
                elif kind == "hardlink":
                    os.link(original, p)
                elif kind == "fifo":
                    os.mkfifo(p, 0o600)
                elif kind == "directory":
                    p.mkdir(mode=0o700)
                else:
                    p.write_bytes(original.read_bytes() if kind != "oversize" else b"x" * 262145)
                    p.chmod(0o644 if kind == "mode" else 0o600)
                # Move the preserved original outside the state directory so
                # the file's own guard is tested, not only the unknown-name gate.
                original.rename(Path(directory) / "original")
                if kind == "unknown":
                    (journal.root / "unknown").write_text("retain")
                with self.assertRaises((ProtocolError, OSError)), journal.claim() as claim:
                    claim.load()
                self.assertTrue(p.exists() or p.is_symlink())

    def test_unsafe_lock_fifo_is_nonblocking_and_closed_claim_denies(self):
        with self.journal.claim() as claim:
            claim.load()
        with self.assertRaisesRegex(ProtocolError, "grok_claim_closed"):
            claim.load()
        (self.root / "lock").rename(self.root.parent / "old-lock")
        os.mkfifo(self.root / "lock", 0o600)
        with self.assertRaises(ProtocolError), self.journal.claim():
            self.fail("FIFO lock accepted")

    def test_partial_fsync_failure_retains_and_denies(self):
        with self.journal.claim() as claim:
            with patch("telegram_bot.core.grok_journal.os.fsync", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError):
                    claim.attempt("one", Baseline(None, ()))
        self.assertEqual(len(list(self.root.glob("pending-*"))), 1)
        with self.assertRaises(ProtocolError), self.journal.claim() as claim:
            claim.load()
        with self.assertRaises(FileExistsError):
            self.journal.create()

    def test_history_chain_and_schema_tampering(self):
        with self.journal.claim() as claim:
            self.operation(claim)
        path = self.root / "0002.json"
        original = path.read_bytes()
        for changes in ({"schema": True}, {"previous": "0" * 64}, {"revision": 999},
                        {"host_version": "other"}, {"operation": None}):
            value = json.loads(original)
            value.update(changes)
            path.write_bytes(canonical(value))
            with self.assertRaises(ProtocolError), self.journal.claim() as claim:
                claim.load()
        path.write_bytes(original)
        with self.journal.claim() as claim:
            self.assertEqual(claim.load()[1], 4)

    def test_transition_and_exact_intent_changes_denied(self):
        with self.journal.claim() as claim:
            op = claim.attempt("one", Baseline(None, ()))
            for candidate in (None, {**op, "stage": "complete"}, {**op, "prompt": "changed"}):
                with self.assertRaises(ProtocolError):
                    claim.append(candidate)
            self.assertEqual(claim.load()[1], 2)

    def test_lock_name_and_directory_replacement_denied(self):
        with self.journal.claim() as claim:
            (self.root / "lock").rename(self.root.parent / "saved-lock")
            (self.root / "lock").write_bytes(b"")
            (self.root / "lock").chmod(0o600)
            with self.assertRaisesRegex(ProtocolError, "grok_lock_changed"):
                claim.load()
        with self.journal.claim() as claim:
            self.root.rename(self.root.parent / "saved-directory")
            self.root.mkdir(mode=0o700)
            with self.assertRaises(ProtocolError):
                claim.load()

    def test_duplicate_nonce_rejected_with_valid_hash_chain(self):
        with self.journal.claim() as claim:
            first = self.operation(claim)
            op = claim.attempt("second", Baseline("reply", ("new",)))
        p = self.root / "0004.json"
        value = json.loads(p.read_bytes())
        value["operation"] = {**first, "stage": "attempted", "accepted": None, "reply": None}
        p.write_bytes(canonical(value))
        with self.assertRaisesRegex(ProtocolError, "grok_nonce_reused"), self.journal.claim() as claim:
            claim.load()
        self.assertNotEqual(op["nonce"], first["nonce"])

    def test_process_claim_and_sigkill_release(self):
        script = '''import json,sys,time
from pathlib import Path
from telegram_bot.core.grok_journal import GrokBinding,GrokJournal
j=GrokJournal(Path(sys.argv[1]),GrokBinding(**json.loads(sys.argv[2])))
with j.claim() as c:
 c.load(); print('claimed',flush=True); time.sleep(30)
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(self.root), json.dumps(asdict(self.binding))],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(4))
            self.assertEqual(child.stdout.readline(), b"claimed\n")
            with self.assertRaisesRegex(ProtocolError, "grok_conversation_busy"), self.journal.claim():
                pass
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=4)
            with self.journal.claim() as claim:
                self.assertEqual(claim.load()[1], 1)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=4)

    def test_whole_valid_prefix_rollback_is_not_detected_claim(self):
        # Explicit limitation: no trusted external monotonic witness exists.
        with self.journal.claim() as claim:
            self.operation(claim)
        saved = self.root.parent / "retained-tail"
        saved.mkdir()
        for p in list(self.root.glob("000[123].json")):
            p.rename(saved / p.name)
        with self.journal.claim() as claim:
            self.assertEqual(claim.load()[1], 1)
        self.assertEqual(len(list(saved.iterdir())), 3)


if __name__ == "__main__":
    unittest.main()
