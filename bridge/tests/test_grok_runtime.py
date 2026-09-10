"""Hermetic real GrokRuntime binding; generated host protocol and private state."""
import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram_bot.core.agent_runtime import SessionRequest
from runtime_conformance import assert_turn_stream_contract
from telegram_bot.core.grok_journal import GrokBinding, GrokJournal
from telegram_bot.core.grok_protocol import HOST_VERSION, ProtocolError, prompt_digest
from telegram_bot.core.grok_runtime import GrokRuntime

AGENT = "00000000-0000-4000-8000-000000000001"


class FakeGrokHost:
    def __init__(self, binding):
        self.destination, self.agent_id = binding.destination, binding.agent_id
        self.rows = [{"id": "initial", "requestId": "prior"}]
        self.acceptances = {}
        self.sends = []
        self.calls = []
        self.fail_after_send = False
        self.busy = False
        self.approval = False
        self.started = asyncio.Event()
        self.before_send = lambda: None
        self.tamper = False

    async def call(self, operation, arguments=None):
        self.calls.append(operation)
        if operation == "status":
            return {"hostVersion": HOST_VERSION, "capabilities": ["orderedReplicasV1", "sendAcceptanceV1"], "isBusy": self.busy}
        if operation == "health":
            return {"ok": True, "isBusy": self.busy, "activeAgentId": self.agent_id, "busyOnlyAwaitingApproval": self.approval}
        if operation == "tail":
            return {"entries": json.loads(json.dumps(self.rows[-64:]))}
        if operation == "send":
            self.before_send()
            nonce, prompt = arguments["nonce"], arguments["prompt"]
            self.sends.append(dict(arguments))
            request = "request-" + str(len(self.sends))
            echo = "echo-" + nonce
            self.rows.extend([
                {"id": echo, "requestId": request, "kind": "message", "role": "user",
                 "content": prompt, "clientNonce": nonce, "isStreaming": False},
                {"id": "reply-" + nonce, "requestId": request if not self.tamper else "foreign",
                 "kind": "send-message", "message": {"type": "text", "content": "generated reply " + prompt}},
            ])
            self.acceptances[nonce] = {"outcome": "found", "record": {
                "accountSlot": "host", "agentId": self.agent_id, "clientNonce": nonce,
                "inputDigest": prompt_digest(self.agent_id, nonce, prompt), "status": "accepted", "echoEntryId": echo}}
            self.started.set()
            if self.fail_after_send:
                raise OSError("synthetic secret body must not escape")
            return {"accepted": True}
        if operation == "acceptance":
            return self.acceptances.get(arguments["nonce"], {"outcome": "not-found"})
        raise AssertionError("unqualified RPC")


class GrokRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "journal"
        self.binding = GrokBinding("box@fixture.invalid", AGENT, "owner-dm", self.tmp.name)
        self.journal = GrokJournal(self.root, self.binding)
        self.journal.create()
        self.host = FakeGrokHost(self.binding)
        self.runtime = GrokRuntime(self.journal, self.host)
        self.request = SessionRequest(self.tmp.name, session_id=self.binding.session_id)
        self.session = await self.runtime.start_or_resume(self.request)

    async def collect(self, message="one", session=None):
        return [e async for e in (session or self.session).send_turn(message)]

    def kinds(self, events):
        return [e.kind for e in events]

    def state(self):
        with self.journal.claim() as c:
            return c.load()[0]

    async def test_actual_runtime_contract_and_commit_before_output(self):
        self.host.before_send = lambda: self.assertEqual(json.loads((self.root / "0001.json").read_text())["operation"]["stage"], "attempted")
        stream = self.session.send_turn("one")
        first = await anext(stream)
        self.assertEqual(first.kind, "text_delta")
        self.assertEqual(self.state()["stage"], "complete")
        events = [first] + [e async for e in stream]
        self.assertEqual(self.kinds(events), ["text_delta", "message_completed", "result", "completion"])
        assert_turn_stream_contract(events)
        self.assertEqual(events[-2].result["text"], "generated reply one")
        self.assertEqual(len(self.host.sends), 1)

    async def test_lost_send_reply_reopen_reconciles_without_send(self):
        self.host.fail_after_send = True
        events = await self.collect()
        self.assertEqual(self.kinds(events), ["error"])
        self.assertFalse(events[0].retryable)
        self.assertNotIn("secret body", events[0].message)
        self.assertEqual(self.state()["stage"], "attempted")
        self.host.fail_after_send = False
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        events = await self.collect(session=reopened)
        self.assertEqual(events[-1].kind, "completion")
        self.assertEqual(len(self.host.sends), 1)

    async def test_result_lost_and_identical_last_input_are_cached(self):
        await self.collect()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        await self.collect(session=reopened)
        self.assertEqual(len(self.host.sends), 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        await self.collect("different", reopened)
        self.assertEqual(len(self.host.sends), 2)

    async def test_crash_before_send_never_auto_submits(self):
        from telegram_bot.core.grok_protocol import capture_baseline
        with self.journal.claim() as c:
            c.attempt("one", capture_baseline({"entries": self.host.rows}))
        events = await self.collect()
        self.assertEqual(self.kinds(events), ["error"])
        self.assertEqual(len(self.host.sends), 0)
        self.assertEqual(self.state()["stage"], "attempted")

    async def test_pending_different_input_and_interference_denied(self):
        self.host.fail_after_send = True
        await self.collect()
        self.host.fail_after_send = False
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        self.assertEqual(self.kinds(await self.collect("other", reopened)), ["error"])
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        self.host.rows[-1]["requestId"] = "foreign"
        self.assertEqual(self.kinds(await self.collect("one", reopened)), ["error"])
        self.assertEqual(len(self.host.sends), 1)

    async def test_options_and_identity_never_fallback(self):
        bad = [replace(self.request, session_id=None), replace(self.request, session_id="fake"),
               replace(self.request, working_directory="/elsewhere")]
        for key, value in (("model", "grok"), ("effort", "high"), ("approval_policy", "never"),
                           ("approvals_reviewer", "user"), ("sandbox_policy", {"type": "sandbox"}),
                           ("memory_environment", {"MEMORY": "fixture"})):
            bad.append(replace(self.request, **{key: value}))
        for request in bad:
            with self.subTest(request=request), self.assertRaises(ProtocolError):
                await self.runtime.start_or_resume(request)
        self.assertEqual(await self.runtime.list_models(), ())
        self.host.agent_id = "00000000-0000-4000-8000-000000000003"
        with self.assertRaises(ProtocolError):
            await self.runtime.start_or_resume(self.request)

    async def test_interrupt_and_reopen_retains_remote_outcome(self):
        self.host.before_send = lambda: setattr(self.host, "busy", True)
        task = asyncio.create_task(self.collect())
        await self.host.started.wait()
        await self.session.interrupt()
        events = await task
        self.assertEqual(events[-1].code, "grok_interrupted_outcome_unknown")
        self.assertNotIn("interrupt", self.host.calls)
        self.host.busy = False
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        self.assertEqual((await self.collect(session=reopened))[-1].kind, "completion")
        self.assertEqual(len(self.host.sends), 1)

    async def test_no_late_output_after_committed_result_interrupt(self):
        stream = self.session.send_turn("one")
        self.assertEqual((await anext(stream)).kind, "text_delta")
        await self.session.interrupt()
        rest = [e async for e in stream]
        self.assertEqual(self.kinds(rest), ["error"])
        self.assertEqual(self.state()["stage"], "complete")

    async def test_same_session_serializes_and_other_instance_denies(self):
        self.host.before_send = lambda: setattr(self.host, "busy", True)
        first = asyncio.create_task(self.collect())
        await self.host.started.wait()
        with self.assertRaisesRegex(ProtocolError, "grok_conversation_busy"):
            await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        second = asyncio.create_task(self.collect("two"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.host.sends), 1)
        self.host.before_send = lambda: None
        self.host.busy = False
        await first
        await second
        self.assertEqual(len(self.host.sends), 2)

    async def test_failed_result_commit_returns_no_text_and_retains_state(self):
        from telegram_bot.core.grok_journal import GrokClaim
        with patch.object(GrokClaim, "complete", side_effect=OSError("synthetic private path")):
            self.assertEqual(self.kinds(await self.collect()), ["error"])
        self.assertEqual(self.state()["stage"], "accepted")
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        self.assertEqual((await self.collect(session=reopened))[-1].kind, "completion")
        self.assertEqual(len(self.host.sends), 1)

    async def test_missing_corrupt_state_cannot_reinitialize(self):
        for p in (self.root / "0000.json",):
            p.write_bytes(b"broken")
            with self.assertRaises(ProtocolError):
                await self.runtime.start_or_resume(self.request)
            self.assertEqual(p.read_bytes(), b"broken")
        other = GrokJournal(self.root.parent / "missing", self.binding)
        with self.assertRaises(FileNotFoundError):
            await GrokRuntime(other, self.host).start_or_resume(self.request)
        self.assertFalse(other.root.exists())

    async def test_tool_record_does_not_fabricate_approval(self):
        self.host.fail_after_send = True
        await self.collect()
        self.host.rows[-1]["message"] = {"type": "approval", "approval": {"action": "fixture"}}
        self.host.fail_after_send = False
        reopened = await GrokRuntime(self.journal, self.host).start_or_resume(self.request)
        self.assertEqual(self.kinds(await self.collect(session=reopened)), ["error"])
        self.assertEqual(self.state()["stage"], "accepted")

    async def test_actual_process_sigkill_before_after_send_and_after_commit(self):
        import selectors
        import signal
        import subprocess
        import sys
        script = '''import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[3])
from test_grok_runtime import FakeGrokHost
from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.grok_journal import GrokBinding,GrokJournal
from telegram_bot.core.grok_runtime import GrokRuntime
binding=GrokBinding(**json.loads(sys.argv[2])); phase=sys.argv[4]; saved=Path(sys.argv[5])
async def stop():
 print('ready',flush=True); await asyncio.Future()
class Host(FakeGrokHost):
 async def call(self,operation,arguments=None):
  if operation=='send' and phase=='before': await stop()
  result=await super().call(operation,arguments)
  if operation=='send':
   fd=os.open(saved,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
   with os.fdopen(fd,'w') as f:
    json.dump({'rows':self.rows,'acceptances':self.acceptances,'sends':self.sends},f);f.flush();os.fsync(f.fileno())
   if phase=='after': await stop()
  return result
async def main():
 r=GrokRuntime(GrokJournal(Path(sys.argv[1]),binding),Host(binding))
 s=await r.start_or_resume(SessionRequest(binding.working_directory,session_id=binding.session_id))
 async for event in s.send_turn('process-fixture'):
  if phase=='complete': await stop()
asyncio.run(main())
'''
        for phase in ("before", "after", "complete"):
            with self.subTest(phase=phase):
                journal = GrokJournal(self.root.parent / phase, self.binding)
                journal.create()
                saved = self.root.parent / (phase + "-host.json")
                child = subprocess.Popen([sys.executable, "-u", "-c", script, str(journal.root),
                    json.dumps(asdict(self.binding)), str(Path(__file__).parent), phase, str(saved)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(child.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(5))
                    self.assertEqual(child.stdout.readline(), b"ready\n")
                    child.send_signal(signal.SIGKILL)
                    child.wait(timeout=3)
                    host = FakeGrokHost(self.binding)
                    if saved.exists():
                        for key, value in json.loads(saved.read_text()).items():
                            setattr(host, key, value)
                    runtime = GrokRuntime(journal, host)
                    session = await runtime.start_or_resume(self.request)
                    events = await self.collect("process-fixture", session)
                    self.assertEqual(events[-1].kind, "error" if phase == "before" else "completion")
                    self.assertEqual(len(host.sends), 0 if phase == "before" else 1)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
