"""Single-source provider capability matrix (#387).

This module is the authoritative declaration of what each agent provider can
do through the bridge.  ``docs/provider-capability-matrix.md`` is rendered
from it and must never be edited by hand; ``tests/test_provider_capabilities.py``
fails when the committed document, this module, and the runtime code drift
apart, and ``tests/test_runtime_conformance.py`` exercises the behavioral
axes against the real runtime adapters.

Regenerate the committed document with:

    python -m telegram_bot.core.provider_capabilities > docs/provider-capability-matrix.md
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType

# Must stay equal to session.manager.SessionManager.VALID_PROVIDERS; the
# capability tests pin the equality so a new provider cannot land without a
# declared capability row.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "codex")

_DEPENDENCY_PATTERN = re.compile(r"^#\d+$")


class CapabilityState(str, Enum):
    """How completely a provider supports one capability axis."""

    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """One provider's machine-readable status for one capability axis."""

    state: CapabilityState
    reason: str
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, CapabilityState):
            raise ValueError(f"invalid capability state: {self.state!r}")
        if not self.reason or not self.reason.strip():
            raise ValueError("capability reason must not be empty")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        for dependency in self.dependencies:
            if not _DEPENDENCY_PATTERN.match(dependency):
                raise ValueError(
                    f"capability dependency must be an issue reference like '#465': "
                    f"{dependency!r}"
                )


@dataclass(frozen=True, slots=True)
class CapabilityAxis:
    """One capability axis shared by every provider column."""

    key: str
    group: str
    title: str
    description: str
    statuses: Mapping[str, CapabilityStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not re.match(r"^[a-z][a-z0-9_]*$", self.key):
            raise ValueError(f"capability axis key must be snake_case: {self.key!r}")
        for value in (self.group, self.title, self.description):
            if not value or not value.strip():
                raise ValueError(f"axis {self.key!r} has an empty group/title/description")
        missing = [name for name in SUPPORTED_PROVIDERS if name not in self.statuses]
        extra = [name for name in self.statuses if name not in SUPPORTED_PROVIDERS]
        if missing or extra:
            raise ValueError(
                f"axis {self.key!r} must declare exactly {SUPPORTED_PROVIDERS}: "
                f"missing={missing} extra={extra}"
            )
        object.__setattr__(self, "statuses", MappingProxyType(dict(self.statuses)))


def _axis(
    key: str,
    group: str,
    title: str,
    description: str,
    *,
    claude: CapabilityStatus,
    codex: CapabilityStatus,
) -> CapabilityAxis:
    return CapabilityAxis(key, group, title, description, {"claude": claude, "codex": codex})


def _supported(reason: str) -> CapabilityStatus:
    return CapabilityStatus(CapabilityState.SUPPORTED, reason)


def _degraded(reason: str, *dependencies: str) -> CapabilityStatus:
    return CapabilityStatus(CapabilityState.DEGRADED, reason, tuple(dependencies))


def _unsupported(reason: str, *dependencies: str) -> CapabilityStatus:
    return CapabilityStatus(CapabilityState.UNSUPPORTED, reason, tuple(dependencies))


def _unknown(reason: str, *dependencies: str) -> CapabilityStatus:
    return CapabilityStatus(CapabilityState.UNKNOWN, reason, tuple(dependencies))


RUNTIME_GROUP = "Runtime behavior"
MEMORY_GROUP = "Memory parity"

CAPABILITY_AXES: tuple[CapabilityAxis, ...] = (
    _axis(
        "runtime_adapter",
        RUNTIME_GROUP,
        "Provider-neutral runtime adapter",
        "Sessions and turns run behind the AgentRuntime seam and pass the "
        "runtime conformance suite.",
        claude=_supported(
            "ClaudeRuntime adapts the Claude Agent SDK to AgentRuntime, passes the "
            "runtime conformance suite, and is the only Claude path since the "
            "#584 slice C-2 cutover removed the legacy direct SDK path and its "
            "kill-switch flag."
        ),
        codex=_supported(
            "CodexRuntime adapts the app-server protocol to AgentRuntime and passes "
            "the runtime conformance suite."
        ),
    ),
    _axis(
        "session_resume",
        RUNTIME_GROUP,
        "Session resume",
        "Re-attach to an existing provider session by its stable id.",
        claude=_supported(
            "SDK sessions resume by persisted session_id "
            "(CCC_RESUME_PERSISTED_SESSIONS) while the transcript exists."
        ),
        codex=_supported(
            "thread/resume re-attaches by thread id and rejects a mismatched "
            "returned thread."
        ),
    ),
    _axis(
        "text_streaming",
        RUNTIME_GROUP,
        "Streaming answer text",
        "User-visible answer text arrives as incremental deltas within a turn.",
        claude=_supported(
            "SDK stream deltas drive Telegram draft updates when "
            "CCC_TELEGRAM_STREAMING is enabled."
        ),
        codex=_supported(
            "item/agentMessage/delta notifications normalize to TextDeltaEvent."
        ),
    ),
    _axis(
        "reasoning_stream",
        RUNTIME_GROUP,
        "Normalized reasoning stream",
        "Provider reasoning is normalized as private ReasoningDeltaEvent and "
        "never delivered to the user.",
        claude=_supported(
            "On the default adapter path, SDK ThinkingBlock content normalizes to "
            "ReasoningDeltaEvent (#599) and the consume loop keeps it private."
        ),
        codex=_supported(
            "item/reasoning textDelta and summaryTextDelta normalize to "
            "ReasoningDeltaEvent and stay private."
        ),
    ),
    _axis(
        "message_boundaries",
        RUNTIME_GROUP,
        "Intra-turn message boundaries",
        "Distinct assistant messages inside one turn are delimited so interim "
        "answers can deliver before tool work continues.",
        claude=_supported(
            "Each SDK AssistantMessage frame is a message boundary in the reader path."
        ),
        codex=_supported(
            "item/completed for agentMessage items normalizes to MessageCompletedEvent."
        ),
    ),
    _axis(
        "tool_event_stream",
        RUNTIME_GROUP,
        "Tool lifecycle events",
        "Tool execution start and completion surface as normalized paired events.",
        claude=_supported(
            "SDK tool_use/tool_result blocks drive tool status in the reader path."
        ),
        codex=_supported(
            "item/started and item/completed normalize to "
            "ToolStartedEvent/ToolCompletedEvent pairs."
        ),
    ),
    _axis(
        "interactive_approvals",
        RUNTIME_GROUP,
        "Interactive approvals",
        "Privileged actions pause for an explicit allow/deny decision; an "
        "omitted or failing handler is fail-closed deny.",
        claude=_supported(
            "SDK permission callbacks gate tool use through Telegram inline approval."
        ),
        codex=_supported(
            "Approval server requests normalize to ApprovalRequestEvent; a missing "
            "or failing handler denies."
        ),
    ),
    _axis(
        "turn_interrupt",
        RUNTIME_GROUP,
        "Turn interrupt",
        "In-flight work can be cancelled; interrupting an idle session is a "
        "safe no-op.",
        claude=_supported(
            "/stop cancels the active task and interrupts the SDK client."
        ),
        codex=_supported(
            "turn/interrupt targets the exact active turn id; interrupted turns "
            "end with error code 'interrupted'."
        ),
    ),
    _axis(
        "turn_serialization",
        RUNTIME_GROUP,
        "Per-session turn serialization",
        "Concurrent sends on one conversation execute strictly one turn at a time.",
        claude=_supported(
            "The conversation FIFO serializes requests until a terminal result "
            "releases the lock."
        ),
        codex=_supported(
            "A per-thread turn lock serializes send_turn calls on the same thread."
        ),
    ),
    _axis(
        "session_browsing",
        RUNTIME_GROUP,
        "Stored-session browsing",
        "Stored sessions can be listed and read back in bounded, normalized form.",
        claude=_supported(
            "/resume lists and previews SDK transcripts from the projects directory."
        ),
        codex=_supported(
            "SessionBrowser is implemented over thread/list and thread/read with "
            "bounded output."
        ),
    ),
    _axis(
        "model_discovery",
        RUNTIME_GROUP,
        "Model discovery",
        "Selectable models are enumerated from the provider at runtime.",
        claude=_degraded(
            "The Claude /model list is a static curated set in the bridge, not "
            "enumerated from the provider."
        ),
        codex=_supported(
            "model/list responses normalize to ModelInfo including "
            "reasoning-effort metadata."
        ),
    ),
    _axis(
        "usage_metering",
        RUNTIME_GROUP,
        "Usage metering",
        "Account/session usage normalizes into provider-tagged UsageSnapshot values.",
        claude=_supported(
            "Rate-limit events and result usage parse via parse_claude_rate_limit_event "
            "and parse_claude_result."
        ),
        codex=_supported(
            "Account rate limits, account usage, and thread token usage parse and "
            "merge into UsageSnapshot."
        ),
    ),
    _axis(
        "terminal_stall_release",
        RUNTIME_GROUP,
        "Terminal-event stall release",
        "A vanished completion event releases the conversation after a bounded "
        "grace with exactly-once buffered answer delivery.",
        claude=_supported(
            "The Claude reader applies CCC_TERMINAL_STALL_SECONDS with exactly-once "
            "buffered delivery (#411)."
        ),
        codex=_supported(
            "The provider-neutral consumer shares the same stall guard and closes "
            "abandoned iterators (#411)."
        ),
    ),
    _axis(
        "memory_session_resume",
        MEMORY_GROUP,
        "Memory: session resume",
        "Conversation context resumes from the provider thread/session id.",
        claude=_supported(
            "Persisted SDK session ids resume with full provider-side context."
        ),
        codex=_supported(
            "Thread ids persist per conversation and resume through thread/resume; "
            "live cold resume verified 2026-07-15."
        ),
    ),
    _axis(
        "memory_read_bootstrap",
        MEMORY_GROUP,
        "Memory: read bootstrap",
        "The MEMORY/USER/local/Wiki/Honcho/resume startup snapshot is recognized "
        "at session start.",
        claude=_supported(
            "SessionStart injects the bounded local snapshot via "
            "claude/hooks/load-memory.sh."
        ),
        codex=_supported(
            "The AGENTS.override.md materializer runs before thread start/resume; "
            "promoted after the 2026-07-15 live gate (#419)."
        ),
    ),
    _axis(
        "memory_postcompact_reinject",
        MEMORY_GROUP,
        "Memory: post-compaction reinjection",
        "Instruction/memory meaning survives compaction and cold resume.",
        claude=_supported(
            "PostCompact re-injects the bounded snapshot through "
            "claude/hooks/load-memory.sh."
        ),
        codex=_degraded(
            "Cold resume re-reads the refreshed global snapshot, but a real provider "
            "compaction event has not been forced in a live gate."
        ),
    ),
    _axis(
        "memory_writeback_distill",
        MEMORY_GROUP,
        "Memory: write-back distill",
        "Durable facts are extracted from provider threads for the memory sinks.",
        claude=_supported(
            "PreCompact/SessionEnd distill transcripts into resume/local facts and "
            "sink candidates via claude/hooks/distill.sh."
        ),
        codex=_degraded(
            "Session-reset, explicit, opt-in bounded checkpoint, and bounded "
            "shutdown-queue triggers, extraction, and the local sink are scheduled. "
            "Provider/model/turn-byte/duration accounting and shared warn/enforce "
            "cost gates are body-free; Wiki candidates enter a local human-review "
            "queue, while Honcho routing remains pending.",
            "#465",
        ),
    ),
    _axis(
        "memory_sink_local",
        MEMORY_GROUP,
        "Memory: local sink",
        "resume/local structured facts are written replay-safe with provenance.",
        claude=_supported(
            "Distill writes resume.md and local structured facts "
            "(claude/hooks/distill/resume-write.sh, local-facts.sh)."
        ),
        codex=_supported(
            "Supported session-reset triggers bind an opaque audience route, and "
            "an independently leased worker writes replay-safe local facts/resume."
        ),
    ),
    _axis(
        "memory_sink_honcho",
        MEMORY_GROUP,
        "Memory: Honcho sink",
        "Redacted conclusions push to Honcho through a durable retry queue.",
        claude=_supported(
            "Redacted payloads push via claude/hooks/distill/honcho-push.sh with the "
            "queue-drain retry path."
        ),
        codex=_unsupported(
            "No Codex-side Honcho routing exists yet.",
            "#465",
        ),
    ),
    _axis(
        "memory_sink_wiki_candidate",
        MEMORY_GROUP,
        "Memory: Wiki candidate sink",
        "Only human-gated Wiki candidates are generated; nothing auto-merges.",
        claude=_supported(
            "Distill emits human-gated Wiki candidates via "
            "claude/hooks/distill/wiki-queue.sh."
        ),
        codex=_supported(
            "Validated candidates are atomically queued in owner-only per-job records; "
            "the sink performs no Wiki write, branch, PR, or merge."
        ),
    ),
    _axis(
        "memory_roundtrip",
        MEMORY_GROUP,
        "Memory: read/write round-trip",
        "A durable fact written in session A is recalled by an isolated later "
        "session B.",
        claude=_supported(
            "SessionEnd write-back feeds the next SessionStart snapshot; both hook "
            "directions carry executable tests (memory-hooks.test.sh, distill/*.test.sh)."
        ),
        codex=_degraded(
            "A hermetic audience-scoped A→distill→local index→B snapshot test "
            "recalls one durable fact exactly once; Honcho parity and an "
            "approved live provider proof remain pending.",
            "#465",
        ),
    ),
)


def _build_matrix() -> Mapping[str, Mapping[str, CapabilityStatus]]:
    keys = [axis.key for axis in CAPABILITY_AXES]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise ValueError(f"duplicate capability axis keys: {sorted(duplicates)}")
    matrix: dict[str, dict[str, CapabilityStatus]] = {
        provider: {} for provider in SUPPORTED_PROVIDERS
    }
    for axis in CAPABILITY_AXES:
        for provider in SUPPORTED_PROVIDERS:
            matrix[provider][axis.key] = axis.statuses[provider]
    return MappingProxyType(
        {provider: MappingProxyType(row) for provider, row in matrix.items()}
    )


PROVIDER_CAPABILITY_MATRIX: Mapping[str, Mapping[str, CapabilityStatus]] = _build_matrix()


def capability_status(provider: str, axis_key: str) -> CapabilityStatus:
    """Return one provider's declared status for one capability axis."""

    try:
        row = PROVIDER_CAPABILITY_MATRIX[provider]
    except KeyError:
        raise KeyError(f"unknown provider: {provider!r}") from None
    try:
        return row[axis_key]
    except KeyError:
        raise KeyError(f"unknown capability axis: {axis_key!r}") from None


def _render_status_cell(status: CapabilityStatus) -> str:
    if status.state is CapabilityState.SUPPORTED:
        return f"`{status.state.value}` — {status.reason}"
    suffix = f" (depends on {', '.join(status.dependencies)})" if status.dependencies else ""
    return f"`{status.state.value}` — {status.reason}{suffix}"


def render_capability_matrix_markdown() -> str:
    """Render the committed capability-matrix document deterministically."""

    lines: list[str] = [
        "# Provider capability matrix",
        "",
        "<!-- Generated from bridge/core/provider_capabilities.py — do not edit by hand.",
        "     Regenerate with:",
        "     python -m telegram_bot.core.provider_capabilities > docs/provider-capability-matrix.md -->",
        "",
        "Single source of truth for what each agent provider supports through the",
        "bridge (#387). The Python module `bridge/core/provider_capabilities.py` is",
        "authoritative; this file is rendered from it and",
        "`bridge/tests/test_provider_capabilities.py` fails CI when they drift apart.",
        "",
        "## States",
        "",
        "| State | Meaning |",
        "|---|---|",
        "| `supported` | Implemented, exercised by tests, and (where noted) verified live. |",
        "| `degraded` | Partially implemented or missing live evidence; the reason names the gap. |",
        "| `unsupported` | Not implemented for this provider yet. |",
        "| `unknown` | Cannot be judged until the named dependency lands. |",
        "",
        "Non-`supported` states carry a machine-readable reason and, where one",
        "exists, the tracking issue dependency.",
        "",
        "## Conformance gate",
        "",
        "The behavioral runtime axes are executable: "
        "`bridge/tests/test_runtime_conformance.py`",
        "runs the shared `AgentRuntime` conformance suite against every adapter",
        "(currently `CodexRuntime` over a scripted fake app-server, plus the",
        "normative in-memory reference runtime) with no live provider calls, and a",
        "negative harness proves that contract-violating runtimes fail the suite.",
        "New provider adapters (#354 successors) must pass this suite and add a",
        "column here before landing.",
        "",
    ]
    for group in (RUNTIME_GROUP, MEMORY_GROUP):
        lines.append(f"## {group}")
        lines.append("")
        lines.append("| Capability | " + " | ".join(SUPPORTED_PROVIDERS) + " |")
        lines.append("|---" * (len(SUPPORTED_PROVIDERS) + 1) + "|")
        for axis in CAPABILITY_AXES:
            if axis.group != group:
                continue
            cells = [
                _render_status_cell(axis.statuses[provider])
                for provider in SUPPORTED_PROVIDERS
            ]
            lines.append(
                f"| `{axis.key}` — {axis.title}: {axis.description} | "
                + " | ".join(cells)
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_capability_matrix_markdown(), end="")
