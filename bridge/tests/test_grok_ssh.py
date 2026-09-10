"""Generated fixtures only; no gateway credentials or live Bots in CI."""
import asyncio
import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from telegram_bot.core import grok_gateway as gateway
from telegram_bot.core.grok_protocol import MAX_WIRE, ProtocolError
from telegram_bot.core.grok_ssh import GrokSshTransport, helper_source

AGENT = "00000000-0000-4000-8000-000000000001"
NONCE = "00000000-0000-4000-8000-000000000002"
SECRET = "synthetic-gateway-token-not-real"


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.directory = self.home / "sand-data"
        self.directory.mkdir(mode=0o700)
        self.config = self.directory / "gateway.json"
        self.config.write_text(json.dumps({"scheme": "http", "port": 1340, "token": SECRET}))
        self.config.chmod(0o644)

    def test_exact_allowlist(self):
        for op, path in (("health", "/health"), ("status", "/api/getHostStatus"),
                         ("tail", "/api/getAgentTranscriptTail")):
            method, actual, _ = gateway.request_spec(op, AGENT, {})
            self.assertEqual(path, actual)
            self.assertEqual(method, "GET" if op == "health" else "POST")
        _, _, raw = gateway.request_spec("send", AGENT, {"nonce": NONCE, "prompt": "테스트"})
        self.assertEqual(json.loads(raw), {"agentId": AGENT, "clientNonce": NONCE, "prompt": "테스트"})
        for op, args in (("interrupt", {}), ("send", {"nonce": NONCE}),
                         ("tail", {"url": "http://invalid"}), ("acceptance", {"nonce": "bad"}),
                         ("health", {"headers": {"Authorization": SECRET}})):
            with self.subTest(op=op), self.assertRaises(ProtocolError):
                gateway.request_spec(op, AGENT, args)

    def test_private_parent_vendor_mode(self):
        self.assertEqual(gateway._gateway_config(self.home)["token"], SECRET)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)
        self.directory.chmod(0o755)
        with self.assertRaises(ProtocolError):
            gateway._gateway_config(self.home)

    def test_unsafe_files_retained(self):
        for kind in ("symlink", "hardlink", "fifo", "writable", "oversize", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                home = Path(root)
                d = home / "sand-data"
                d.mkdir(mode=0o700)
                p = d / "gateway.json"
                if kind == "symlink":
                    p.symlink_to(self.config)
                elif kind == "hardlink":
                    os.link(self.config, p)
                elif kind == "fifo":
                    os.mkfifo(p, 0o600)
                elif kind == "directory":
                    p.mkdir()
                else:
                    p.write_bytes(b"x" * (16385 if kind == "oversize" else 10))
                    p.chmod(0o666 if kind == "writable" else 0o600)
                with self.assertRaises((ProtocolError, OSError)):
                    gateway._gateway_config(home)
                self.assertTrue(p.exists())

    def test_parent_symlink_and_configuration_denial(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "sand-data").symlink_to(self.directory)
            with self.assertRaises(OSError):
                gateway._gateway_config(Path(root))
        for changes in ({"port": True}, {"port": 443}, {"scheme": "https"},
                        {"token": SECRET + "\r\nOther: injected"}, {"token": "short"}):
            value = {"scheme": "http", "port": 1340, "token": SECRET, **changes}
            self.config.write_text(json.dumps(value))
            with self.assertRaises(ProtocolError):
                gateway._gateway_config(self.home)

    def test_fixed_loopback_no_redirect_and_bounded_response(self):
        class Connection:
            def __init__(self, host, port, timeout):
                self.args = (host, port, timeout)
                self.closed = False
            def request(self, *args, **kwargs):
                self.request_args = (args, kwargs)
            def getresponse(self):
                return self
            def read(self, limit):
                self.limit = limit
                return b'{"ok":true}'
            def close(self):
                self.closed = True
        conn = Connection("127.0.0.1", 1340, 10)
        conn.status = 200
        with patch.object(gateway.Path, "home", return_value=self.home), \
                patch.object(gateway.http.client, "HTTPConnection", return_value=conn) as factory:
            self.assertEqual(gateway.gateway_call("health", AGENT, {}), {"ok": True})
            factory.assert_called_once_with("127.0.0.1", 1340, timeout=10)
            self.assertEqual(conn.limit, MAX_WIRE + 1)
            self.assertTrue(conn.closed)
            conn.status = 302
            with self.assertRaisesRegex(ProtocolError, "gateway_http_failure"):
                gateway.gateway_call("health", AGENT, {})

    def test_main_errors_never_return_body_or_token(self):
        request = {"operation": "health", "agent_id": AGENT, "arguments": {}}
        with patch.object(gateway, "gateway_call", side_effect=RuntimeError(SECRET)), \
                patch.object(gateway.os, "write") as write, self.assertRaises(SystemExit):
            gateway.gateway_main(request)
        write.assert_called_once_with(2, b"grok_gateway_failure\n")

    def test_generated_source_is_data_not_code(self):
        prompt = "');raise RuntimeError('INJECTED') #\n테스트"
        raw = helper_source("send", AGENT, {"nonce": NONCE, "prompt": prompt})
        # Execute the original generated script after replacing only its HTTP
        # entry with a local echo. No token or config is required by this probe.
        at = raw.rindex(b"\ngateway_main(decode_wire(")
        shim = b'\ndef gateway_main(r):\n print(json.dumps(r, ensure_ascii=True))\n'
        run = subprocess.run([sys.executable, "-I", "-"], input=raw[:at] + shim + raw[at:],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["arguments"]["prompt"], prompt)


class SshTests(unittest.IsolatedAsyncioTestCase):
    async def run_child(self, code, operation="health", arguments=None):
        original = asyncio.create_subprocess_exec
        children = []
        self.children = children
        async def spawn(*argv, **kwargs):
            self.assertEqual(argv[-1], "python3 -I -")
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIs(kwargs.get("start_new_session"), True)
            self.assertNotIn(SECRET, str(argv))
            p = await original(sys.executable, "-I", "-c", code, **kwargs)
            children.append(p)
            return p
        try:
            with patch("telegram_bot.core.grok_ssh.asyncio.create_subprocess_exec", side_effect=spawn):
                return await GrokSshTransport("box@fixture.invalid", AGENT).call(operation, arguments)
        finally:
            self.assertTrue(all(p.returncode is not None for p in children))

    async def test_success_no_request_in_argv(self):
        value = await self.run_child('import sys;sys.stdin.buffer.read();print("{\\"ok\\":true}")')
        self.assertEqual(value, {"ok": True})

    async def test_failure_and_output_limits_no_body(self):
        for code in (
                'import sys;sys.stdin.buffer.read();sys.stderr.write("' + SECRET + '");sys.exit(1)',
                'import sys;sys.stdin.buffer.read();sys.stdout.write("x" * 1048577)',
                'import sys;sys.stdin.buffer.read();sys.stderr.write("x" * 4097)',
                'import sys;sys.stdin.buffer.read();print("not-json-' + SECRET + '")'):
            with self.subTest(code=code), self.assertRaisesRegex(ProtocolError, "^ssh_outcome_unknown$"):
                await self.run_child(code)

    async def test_cancellation_reaps_ssh_not_claiming_bot_interrupt(self):
        task = asyncio.create_task(self.run_child(
            'import sys,time;sys.stdin.buffer.read();time.sleep(30)'))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_destination_injection_denied_before_spawn(self):
        for dest in ("-oProxyCommand=id", "box@host;id", "box@host $(id)", "box@/tmp/socket", "root"):
            with self.subTest(dest=dest), self.assertRaises(ProtocolError):
                GrokSshTransport(dest, AGENT)

    async def test_cancel_inherited_descendant_pipes(self):
        # The child lives in the caller-owned SSH process group and inherits
        # both pipes. Old parent-only kill blocked until its 15-second exit.
        with tempfile.TemporaryDirectory() as root:
            marker = str(Path(root) / "child-started")
            code = ('import os,sys,time;sys.stdin.buffer.read();p=os.fork();'
                    f'open({marker!r},"w").write(str(os.getpid())) if p==0 else None;'
                    'time.sleep(15)')
            task = asyncio.create_task(self.run_child(code))
            for _ in range(200):
                if Path(marker).exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(Path(marker).exists())
            start = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
            self.assertLess(time.monotonic() - start, 3)

    async def test_spawn_error_categorical(self):
        with patch("telegram_bot.core.grok_ssh.asyncio.create_subprocess_exec",
                   side_effect=OSError(SECRET)), self.assertRaisesRegex(ProtocolError, "^ssh_launch_failure$"):
            await GrokSshTransport("box@fixture.invalid", AGENT).call("health")

    async def test_escaped_test_helper_bounds_local_pipe_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "escaped-child"
            code = ('import os,sys,time;sys.stdin.buffer.read();p=os.fork();'
                    'os.setsid() if p==0 else None;'
                    f'open({str(marker)!r},"w").write(str(os.getpid())) if p==0 else None;'
                    'time.sleep(15)')
            task = asyncio.create_task(self.run_child(code))
            try:
                for _ in range(200):
                    if marker.exists() and marker.read_text():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(marker.exists())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 4)
            finally:
                # This fixture deliberately escapes ownership; only its creator
                # test knows its PID. Production never kills an unowned group.
                if marker.exists() and marker.read_text():
                    try:
                        os.kill(int(marker.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    async def test_exited_parent_owned_or_escaped_pipe_cleanup(self):
        for escaped in (False, True):
            with self.subTest(escaped=escaped), tempfile.TemporaryDirectory() as root:
                marker = Path(root) / "child"
                code = ('import os,sys,time;sys.stdin.buffer.read();p=os.fork();'
                        'os._exit(0) if p else None;'
                        + ('os.setsid();' if escaped else '') +
                        f'open({str(marker)!r},"w").write(str(os.getpid()));time.sleep(15)')
                task = asyncio.create_task(self.run_child(code))
                try:
                    for _ in range(200):
                        if (marker.exists() and marker.read_text() and self.children
                                and self.children[0].returncode == 0):
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(marker.exists())
                    process = self.children[0]
                    self.assertEqual(process.returncode, 0)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 4)
                    transport = getattr(process, "_transport")
                    self.assertTrue(transport.is_closing())
                    for fd in (0, 1, 2):
                        pipe = transport.get_pipe_transport(fd)
                        if pipe:
                            self.assertTrue(pipe.is_closing())
                finally:
                    if marker.exists() and marker.read_text():
                        try:
                            os.kill(int(marker.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
