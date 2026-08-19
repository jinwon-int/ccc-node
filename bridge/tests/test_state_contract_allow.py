"""Working-state checkpoint contract allow (#1045 proposal 1).

The harness requires every session to keep ``working-state.md`` current, yet
the turns that need it most (CI-wait / external_event continuations) have no
approval route, so a Claude ``can_use_tool`` escalation for exactly that file
failed closed — ``denied-no-route`` with ``turn=none`` (yukson 2026-08-08) or
``no-approval-callback`` → ``handler-deny`` with an active turn (gwakga
2026-08-08, 08-17, 08-19). These tests pin the predicate's narrowness and
prove the runtime allows only that file, in both fail-closed branches, with a
body-free ``state-contract-allow`` trace, and that the kill-switch restores
the previous behaviour byte-for-byte.

Runtime-generation note: as in ``test_runtime_approval_observability``, the
runtime modules are resolved at test run time and results are asserted by
class NAME, so collection order does not matter.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

_SECRET_MARKER = "STATE-CONTRACT-SECRET-MARKER"


def _sc():
    return importlib.import_module("telegram_bot.core.state_contract")


def _claude_runtime():
    return importlib.import_module("telegram_bot.core.claude_runtime")


def _agent_runtime():
    return importlib.import_module("telegram_bot.core.agent_runtime")


class PredicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.contract = self.state / "working-state.md"
        self.contract.write_text("objective\n", encoding="utf-8")
        self.candidates = (self.contract,)

    def _allows(self, action, args, **kw):
        return _sc().state_contract_allows(action, args, candidates=self.candidates, **kw)

    def test_structured_write_to_contract_file_allows(self) -> None:
        for action in ("Write", "Edit", "MultiEdit"):
            with self.subTest(action=action):
                self.assertTrue(self._allows(action, {"file_path": str(self.contract)}))

    def test_missing_contract_file_still_matches_by_path(self) -> None:
        # A first checkpoint write creates the file; realpath of a missing
        # leaf resolves through the existing parent.
        self.contract.unlink()
        self.assertTrue(self._allows("Write", {"file_path": str(self.contract)}))

    def test_bash_and_other_actions_never_match(self) -> None:
        for action in ("Bash", "Read", "NotebookEdit", "", None):
            with self.subTest(action=action):
                self.assertFalse(self._allows(action, {"file_path": str(self.contract)}))
                self.assertFalse(
                    self._allows(action, {"command": f"echo x > {self.contract}"})
                )

    def test_sibling_relative_and_malformed_paths_refuse(self) -> None:
        cases = [
            {"file_path": str(self.state / "resume.md")},
            {"file_path": str(self.state / "checkpoints" / "working-state.md")},
            {"file_path": str(self.state) + "/working-state.md.bak"},
            {"file_path": "working-state.md"},
            {"file_path": "state/working-state.md"},
            {"file_path": str(self.contract) + "\x00"},
            {"file_path": ""},
            {"file_path": 42},
            {"path": str(self.contract)},
            {},
            None,
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(self._allows("Write", args))

    def test_dot_segments_resolve_to_the_same_file(self) -> None:
        dotted = str(self.state / "checkpoints" / ".." / "working-state.md")
        (self.state / "checkpoints").mkdir()
        self.assertTrue(self._allows("Write", {"file_path": dotted}))

    def test_symlinked_contract_file_refuses(self) -> None:
        # The contract path itself is a link pointing elsewhere: the write
        # would land outside the contract, so the predicate must refuse and
        # let the fail-closed route decide.
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        self.contract.unlink()
        os.symlink(outside, self.contract)
        self.assertFalse(self._allows("Write", {"file_path": str(self.contract)}))
        self.assertFalse(self._allows("Write", {"file_path": str(outside)}))

    def test_symlinked_request_path_to_contract_file_allows(self) -> None:
        # Request through a link that resolves onto the real contract file:
        # realpath equality holds and the real file is the contract.
        alias = Path(self.tmp.name) / "alias.md"
        os.symlink(self.contract, alias)
        self.assertTrue(self._allows("Write", {"file_path": str(alias)}))

    def test_kill_switch_refuses(self) -> None:
        self.assertFalse(self._allows("Write", {"file_path": str(self.contract)}, enabled=False))

    def test_enabled_resolution_settings_then_env_then_default(self) -> None:
        sc = _sc()
        self.assertTrue(sc.state_contract_enabled(None, environ={}))
        self.assertFalse(sc.state_contract_enabled(None, environ={sc.KILL_SWITCH_ENV: "0"}))
        self.assertFalse(sc.state_contract_enabled(None, environ={sc.KILL_SWITCH_ENV: "off"}))
        self.assertTrue(sc.state_contract_enabled(None, environ={sc.KILL_SWITCH_ENV: "1"}))
        settings_off = SimpleNamespace(state_contract_allow_enabled=False)
        self.assertFalse(sc.state_contract_enabled(settings_off, environ={sc.KILL_SWITCH_ENV: "1"}))
        settings_on = SimpleNamespace(state_contract_allow_enabled=True)
        self.assertTrue(sc.state_contract_enabled(settings_on, environ={sc.KILL_SWITCH_ENV: "0"}))

    def test_contract_files_default_and_scoped_dirs(self) -> None:
        sc = _sc()
        home = Path(self.tmp.name) / "home"
        env = {"HOME": str(home)}
        files = sc.contract_files(None, environ=env)
        self.assertEqual(files, (home / ".claude" / "state" / "working-state.md",))
        env_state = {"HOME": str(home), "CCC_STATE_DIR": str(self.state)}
        self.assertEqual(sc.contract_files(None, environ=env_state), (self.contract,))
        settings = SimpleNamespace(claude_settings_path=str(home / "cfg" / "settings.json"))
        self.assertEqual(
            sc.contract_files(settings, environ=env),
            (home / "cfg" / "state" / "working-state.md",),
        )
        scoped = Path(self.tmp.name) / "aud" / "private-x" / "state"
        files = sc.contract_files(
            settings, extra_state_dirs=(str(scoped), None, "", str(scoped)), environ=env
        )
        self.assertEqual(
            files,
            (
                home / "cfg" / "state" / "working-state.md",
                scoped / "working-state.md",
            ),
        )


def _session(handler=None, *, settings=None, contract_dirs=()):
    cr = _claude_runtime()
    session = cr.ClaudeSession(cr.ClaudeRuntime(settings=settings), "sid")
    session._contract_state_dirs = tuple(contract_dirs)
    if handler is not None:
        session._active_turn = cr._ActiveTurn(
            queue=asyncio.Queue(),
            approval_handler=handler,
            generation=session._turn_generation,
        )
    return session


class ClaudeRuntimeContractAllowTests(unittest.IsolatedAsyncioTestCase):
    """Both fail-closed branches of ``_handle_permission_request`` honour it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.contract = self.state / "working-state.md"
        self.settings = SimpleNamespace(
            claude_settings_path=str(Path(self.tmp.name) / "settings.json"),
            state_contract_allow_enabled=True,
        )
        # Pin the process-level CCC_STATE_DIR away from the real node state.
        self._old_env = os.environ.get("CCC_STATE_DIR")
        os.environ["CCC_STATE_DIR"] = str(self.state)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("CCC_STATE_DIR", None)
        else:
            os.environ["CCC_STATE_DIR"] = self._old_env

    async def _request(self, session, tool="Write", path=None):
        return await session._handle_permission_request(
            tool,
            {"file_path": str(path or self.contract), "content": _SECRET_MARKER},
            SimpleNamespace(tool_use_id="approval-sc-1", title=None),
        )

    async def test_no_route_turn_allows_contract_file_with_trace(self) -> None:
        session = _session(settings=self.settings)  # turn=none
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultAllow")
        joined = "\n".join(logs.output)
        self.assertIn("outcome=allowed reason=state-contract-allow", joined)
        self.assertIn("turn=none", joined)
        self.assertNotIn(_SECRET_MARKER, joined)
        self.assertNotIn(str(self.contract), joined)

    async def test_no_route_turn_still_denies_other_paths(self) -> None:
        session = _session(settings=self.settings)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session, path=self.state / "resume.md")
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertIn("outcome=denied-no-route", "\n".join(logs.output))
        self.assertNotIn("state-contract-allow", "\n".join(logs.output))

    async def test_active_turn_without_callback_allows_contract_without_handler(self) -> None:
        ar = _agent_runtime()
        calls: list[object] = []

        async def deny(request):  # stands in for no-approval-callback → DENY
            calls.append(request)
            return ar.ApprovalDecision.DENY

        session = _session(deny, settings=self.settings)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session, tool="Edit")
        self.assertEqual(type(result).__name__, "PermissionResultAllow")
        self.assertEqual(calls, [])  # never reached the handler
        self.assertTrue(session._active_turn.queue.empty())  # no ApprovalRequestEvent queued
        joined = "\n".join(logs.output)
        self.assertIn("turn=active outcome=allowed reason=state-contract-allow", joined)

    async def test_active_turn_other_path_keeps_handler_deny(self) -> None:
        ar = _agent_runtime()

        async def deny(_request):
            return ar.ApprovalDecision.DENY

        session = _session(deny, settings=self.settings)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session, path=Path(self.tmp.name) / "other.md")
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertIn("reason=handler-deny", result.message)
        self.assertIn("outcome=denied reason=handler-deny", "\n".join(logs.output))

    async def test_bash_redirect_to_contract_file_is_not_auto_allowed(self) -> None:
        session = _session(settings=self.settings)
        result = await session._handle_permission_request(
            "Bash",
            {"command": f"echo x >> {self.contract}"},
            SimpleNamespace(tool_use_id="approval-sc-2", title=None),
        )
        self.assertEqual(type(result).__name__, "PermissionResultDeny")

    async def test_kill_switch_restores_fail_closed_deny(self) -> None:
        self.settings.state_contract_allow_enabled = False
        session = _session(settings=self.settings)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertIn("outcome=denied-no-route", "\n".join(logs.output))

    async def test_scoped_session_contract_file_allows_and_sibling_scope_refuses(self) -> None:
        scoped = Path(self.tmp.name) / "aud" / "private-aaaa" / "state"
        other = Path(self.tmp.name) / "aud" / "private-bbbb" / "state"
        scoped.mkdir(parents=True)
        other.mkdir(parents=True)
        session = _session(settings=self.settings, contract_dirs=(str(scoped),))
        allowed = await self._request(session, path=scoped / "working-state.md")
        self.assertEqual(type(allowed).__name__, "PermissionResultAllow")
        refused = await self._request(session, path=other / "working-state.md")
        self.assertEqual(type(refused).__name__, "PermissionResultDeny")

    async def test_shared_audience_session_excludes_unscoped_contract_file(self) -> None:
        scoped = Path(self.tmp.name) / "aud" / "shared" / "state"
        scoped.mkdir(parents=True)
        session = _session(settings=self.settings, contract_dirs=(str(scoped),))
        session._contract_include_default = False
        allowed = await self._request(session, path=scoped / "working-state.md")
        self.assertEqual(type(allowed).__name__, "PermissionResultAllow")
        refused = await self._request(session)  # node's unscoped checkpoint
        self.assertEqual(type(refused).__name__, "PermissionResultDeny")

    async def test_start_or_resume_records_scoped_state_dir(self) -> None:
        cr = _claude_runtime()
        captured: dict[str, object] = {}

        class _FakeClient:
            async def connect(self) -> None:  # pragma: no cover - not reached
                raise RuntimeError("stop before connect")

        runtime = cr.ClaudeRuntime(settings=self.settings)
        original_build = runtime._build_options

        def _spy_build(request, handler, **kwargs):
            captured["handler_self"] = getattr(handler, "__self__", None)
            raise RuntimeError("stop after session construction")

        runtime._build_options = _spy_build  # type: ignore[method-assign]
        for audience, include_default in (("private", True), ("shared", False)):
            with self.subTest(audience=audience):
                request = cr.SessionRequest(
                    working_directory=self.tmp.name,
                    memory_environment={
                        "CCC_STATE_DIR": str(self.state / "scoped"),
                        "CCC_MEMORY_AUDIENCE": audience,
                    },
                )
                with self.assertRaises(RuntimeError):
                    await runtime.start_or_resume(request)
                session = captured["handler_self"]
                self.assertIsNotNone(session)
                self.assertEqual(session._contract_state_dirs, (str(self.state / "scoped"),))
                self.assertIs(session._contract_include_default, include_default)
        runtime._build_options = original_build  # type: ignore[method-assign]


if __name__ == "__main__":
    unittest.main()
