"""Deny-reason observability for the approval route (#1045).

A headless (external_event) turn's denied tool call used to be a black box:
the agent saw one generic "Denied by the bridge approval handler" message and
the journal showed nothing. Every fail-closed deny now names its decision
point with a body-free reason code — in the Claude runtime's agent-visible
message and INFO trace, and in the provider-neutral approval route's INFO
trace. These tests pin the reason codes and prove the traces never leak
request arguments.

Runtime-generation note: other test modules purge/re-import the SDK and the
runtime modules (see ``test_runtime_conformance``). Every module reference
here is resolved at test run time via ``importlib.import_module`` and results
are asserted by class NAME, not identity, so this module is safe at any
collection position.
"""

from __future__ import annotations

import asyncio
import importlib
import unittest
from types import SimpleNamespace

_SECRET_MARKER = "OBSERVABILITY-SECRET-MARKER"


def _claude_runtime():
    return importlib.import_module("telegram_bot.core.claude_runtime")


def _process_module():
    return importlib.import_module("telegram_bot.core.project_chat_process")


def _agent_runtime():
    return importlib.import_module("telegram_bot.core.agent_runtime")


def _session_with_turn(handler):
    cr = _claude_runtime()
    session = cr.ClaudeSession(cr.ClaudeRuntime(), "sid")
    turn = cr._ActiveTurn(
        queue=asyncio.Queue(),
        approval_handler=handler,
        generation=session._turn_generation,
    )
    session._active_turn = turn
    return session, turn


class ClaudeDenyReasonTests(unittest.IsolatedAsyncioTestCase):
    """``_handle_permission_request`` names every deny decision point."""

    async def _request(self, session):
        return await session._handle_permission_request(
            "Write",
            {"file_path": "/tmp/x", "content": _SECRET_MARKER},
            SimpleNamespace(tool_use_id="approval-obs-1", title=None),
        )

    async def test_handler_deny_reason(self) -> None:
        ar = _agent_runtime()

        async def deny(_request):
            return ar.ApprovalDecision.DENY

        session, _turn = _session_with_turn(deny)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertIn("reason=handler-deny", result.message)
        joined = "\n".join(logs.output)
        self.assertIn("outcome=denied reason=handler-deny", joined)
        self.assertNotIn(_SECRET_MARKER, joined)

    async def test_handler_exception_reason(self) -> None:
        async def boom(_request):
            raise RuntimeError(_SECRET_MARKER)

        session, _turn = _session_with_turn(boom)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertIn("reason=handler-exception", result.message)
        joined = "\n".join(logs.output)
        self.assertIn("outcome=denied reason=handler-exception", joined)
        # The warning names the exception CLASS only — never its message,
        # which may carry request content.
        self.assertIn("RuntimeError", joined)
        self.assertNotIn(_SECRET_MARKER, joined)
        self.assertNotIn(_SECRET_MARKER, result.message)

    async def test_turn_superseded_reason(self) -> None:
        ar = _agent_runtime()
        session_box: list = []

        async def allow_but_finish(_request):
            session_box[0]._active_turn.finished = True
            return ar.ApprovalDecision.ALLOW

        session, _turn = _session_with_turn(allow_but_finish)
        session_box.append(session)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertIn("reason=turn-superseded", result.message)
        self.assertIn("outcome=denied reason=turn-superseded", "\n".join(logs.output))

    async def test_allow_keeps_allowing_with_placeholder_reason(self) -> None:
        ar = _agent_runtime()

        async def allow(_request):
            return ar.ApprovalDecision.ALLOW

        session, _turn = _session_with_turn(allow)
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultAllow")
        self.assertIn("outcome=allowed reason=-", "\n".join(logs.output))

    async def test_no_route_message_is_unchanged(self) -> None:
        cr = _claude_runtime()
        session = cr.ClaudeSession(cr.ClaudeRuntime(), "sid")
        with self.assertLogs("telegram_bot.core.claude_runtime", level="INFO") as logs:
            result = await self._request(session)
        self.assertEqual(type(result).__name__, "PermissionResultDeny")
        self.assertEqual(result.message, cr._NO_ACTIVE_APPROVAL_ROUTE)
        self.assertIn("outcome=denied-no-route", "\n".join(logs.output))


class ApprovalRouteDenyTraceTests(unittest.TestCase):
    """``_log_approval_route_deny`` is body-free and complete."""

    def _event(self):
        ar = _agent_runtime()
        return ar.ApprovalRequestEvent(
            request_id="req-obs-1",
            action="Write",
            arguments={"file_path": "/tmp/x", "content": _SECRET_MARKER},
            description=f"write {_SECRET_MARKER}",
        )

    def test_logs_reason_and_route_ids_without_arguments(self) -> None:
        proc = _process_module()
        with self.assertLogs(
            "telegram_bot.core.project_chat_process", level="INFO"
        ) as logs:
            proc._log_approval_route_deny(
                reason="no-approval-callback",
                event=self._event(),
                user_id=11,
                chat_id=22,
                generation=3,
            )
        joined = "\n".join(logs.output)
        self.assertIn("Approval route denied reason=no-approval-callback", joined)
        self.assertIn("action=Write", joined)
        self.assertIn("request_id=req-obs-1", joined)
        self.assertIn("user_id=11", joined)
        self.assertIn("chat_id=22", joined)
        self.assertIn("generation=3", joined)
        # Body-free: neither arguments nor the human description may leak.
        self.assertNotIn(_SECRET_MARKER, joined)

    def test_tolerates_missing_event(self) -> None:
        proc = _process_module()
        with self.assertLogs(
            "telegram_bot.core.project_chat_process", level="INFO"
        ) as logs:
            proc._log_approval_route_deny(
                reason="approval-inactive",
                event=None,
                user_id=1,
                chat_id=1,
                generation=0,
            )
        self.assertIn("reason=approval-inactive", "\n".join(logs.output))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
