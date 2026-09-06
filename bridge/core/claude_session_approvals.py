"""Claude SDK permission requests bridged to one active session turn."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, cast

from claude_agent_sdk import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .agent_runtime import (
    ApprovalDecision,
    ApprovalRequestEvent,
    ApprovalResolvedEvent,
    JsonValue,
    approval_target_kind,
)
from .state_contract import (
    ALLOW_REASON as _STATE_CONTRACT_ALLOW_REASON,
    PATH_ALLOW_REASON as _STATE_PATH_ALLOW_REASON,
    contract_files as _state_contract_files,
    path_allow_roots as _state_path_allow_roots,
    state_contract_allows as _state_contract_allows,
    state_contract_enabled as _state_contract_enabled,
    state_path_allow_enabled as _state_path_allow_enabled,
    state_path_allows as _state_path_allows,
)

if TYPE_CHECKING:
    from .claude_runtime import ClaudeRuntime, _ActiveTurn


# Preserve the established body-free trace channel after this pure move.
logger = logging.getLogger("telegram_bot.core.claude_runtime")

_NO_ACTIVE_APPROVAL_ROUTE = (
    "No active turn accepts approval requests; start a new user turn and retry"
)


def _approval_target_kind(tool_input: object) -> str:
    """Body-free shape hint; the shared helper lives in ``agent_runtime`` (#1555)."""

    return approval_target_kind(tool_input)


class ClaudeSessionApprovalMixin:
    """Bridge Claude SDK permission callbacks to the active turn handler."""

    _runtime: ClaudeRuntime
    _active_turn: _ActiveTurn | None
    _turn_generation: int
    _approval_counter: int
    _contract_state_dirs: tuple[str, ...]
    _contract_include_default: bool

    def _state_contract_allows(self, tool_name: str, tool_input: object) -> bool:
        settings = getattr(self._runtime, "_settings", None)
        if not _state_contract_enabled(settings):
            return False
        candidates = _state_contract_files(
            settings,
            extra_state_dirs=self._contract_state_dirs,
            include_default=self._contract_include_default,
        )
        return _state_contract_allows(
            tool_name,
            tool_input if isinstance(tool_input, Mapping) else None,
            candidates=candidates,
            enabled=True,
        )

    def _state_path_allows(self, tool_name: str, tool_input: object) -> bool:
        settings = getattr(self._runtime, "_settings", None)
        if not _state_path_allow_enabled(settings):
            return False
        roots = _state_path_allow_roots(
            settings,
            extra_state_dirs=self._contract_state_dirs,
            include_default=self._contract_include_default,
        )
        return _state_path_allows(
            tool_name,
            tool_input if isinstance(tool_input, Mapping) else None,
            roots=roots,
            enabled=True,
        )

    def _auto_allow_reason(self, tool_name: str, tool_input: object) -> str | None:
        """Body-free reason code when a request needs no approval route, else None."""

        if self._state_contract_allows(tool_name, tool_input):
            return _STATE_CONTRACT_ALLOW_REASON
        if self._state_path_allows(tool_name, tool_input):
            return _STATE_PATH_ALLOW_REASON
        return None

    async def _handle_permission_request(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        """SDK ``can_use_tool`` callback bridged to the per-turn approval handler.

        Fail-closed: without an in-flight turn, or when the turn's handler
        raises, the provider receives a deny decision.
        """

        # #1045 proposal 1: the working-state checkpoint contract file is the
        # one path a turn may write without an approval route. Evaluated
        # before BOTH fail-closed branches below (turn=none no-route and the
        # active-turn handler that has no callback in headless continuations),
        # so the harness contract "keep working-state.md current" holds in
        # exactly the turns that need it. Narrow by construction: structured
        # write actions, absolute file_path, realpath-equal to a known
        # non-symlinked contract file, kill-switch CCC_STATE_CONTRACT_ALLOW.
        # #1555 extends the same shape to files under the state dir(s), /tmp
        # and ~/ccc-wt (reason=state-path-allow, kill-switch
        # CCC_STATE_PATH_ALLOW). Anything else continues into the unchanged
        # fail-closed flow.
        auto_allow_reason = self._auto_allow_reason(tool_name, tool_input)
        if auto_allow_reason is not None:
            active_for_log = self._active_turn
            logger.info(
                "Approval request allowed provider=claude tool=%s target_kind=path "
                "request_id=%s turn=%s outcome=allowed reason=%s",
                tool_name,
                getattr(context, "tool_use_id", None),
                "none" if active_for_log is None or active_for_log.finished else "active",
                auto_allow_reason,
            )
            return PermissionResultAllow()

        active = self._active_turn
        if active is None or active.finished:
            logger.info(
                "Approval request denied (no active route) provider=claude "
                "tool=%s target_kind=%s request_id=%s turn=none outcome=denied-no-route",
                tool_name,
                _approval_target_kind(tool_input),
                getattr(context, "tool_use_id", None),
            )
            return PermissionResultDeny(message=_NO_ACTIVE_APPROVAL_ROUTE)
        generation = active.generation
        self._approval_counter += 1
        request_id = context.tool_use_id or f"approval-{self._approval_counter}"
        request = ApprovalRequestEvent(
            request_id=request_id,
            action=tool_name,
            arguments=cast(Mapping[str, JsonValue], tool_input),
            description=context.title or f"Claude requests permission to use {tool_name}",
        )
        active.queue.put_nowait(request)
        # #1045: a fail-closed deny used to be indistinguishable from an
        # explicit handler deny — one generic message, one log shape. Headless
        # (external_event) turns hit exactly these branches, so every deny now
        # carries its decision point as a body-free reason code, in both the
        # agent-visible message and the INFO trace. Never the request content.
        deny_reason: str | None = None
        try:
            decision = await active.approval_handler(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decision = ApprovalDecision.DENY
            deny_reason = "handler-exception"
            logger.warning(
                "Claude approval handler raised %s; denying (fail-closed) "
                "request_id=%s",
                type(exc).__name__,
                request_id,
            )
        if (
            active.finished
            or self._turn_generation != generation
            or self._active_turn is not active
        ):
            decision = ApprovalDecision.DENY
            deny_reason = deny_reason or "turn-superseded"
        outcome = "allowed" if decision is ApprovalDecision.ALLOW else "denied"
        if decision is not ApprovalDecision.ALLOW:
            deny_reason = deny_reason or "handler-deny"
        # #1555: the request event above set the turn's approval_pending lease;
        # settle it here. A delegated (sub-agent) tool call has its own frames
        # filtered from the turn stream, so without this event nothing cleared
        # the lease and the approval-stall guard released a healthy turn.
        active.queue.put_nowait(
            ApprovalResolvedEvent(request_id=request_id, action=tool_name, decision=decision)
        )
        logger.info(
            "Approval request provider=claude tool=%s target_kind=%s "
            "request_id=%s turn=active outcome=%s reason=%s",
            tool_name,
            _approval_target_kind(tool_input),
            request_id,
            outcome,
            deny_reason or "-",
        )
        if decision is ApprovalDecision.ALLOW:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(
                "Denied by the bridge approval handler "
                f"(reason={deny_reason}; deny trace is in the bridge log)"
            )
        )
